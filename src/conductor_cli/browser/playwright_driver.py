"""Playwright CDP adapter for Chromium started by this managed owner."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .references import ReferenceRegistry, StaleReferenceError
from .service import BrowserCommandError
from .storage import read_storage_state

MAX_FRAMES = 8
MAX_ELEMENTS = 80
_DOC_TOKEN_JS = """() => {
  const key = '__conductor_agent_browser_doc';
  if (!window[key]) Object.defineProperty(window, key, {value: `${Date.now()}-${Math.random()}-${Math.random()}`});
  return window[key];
}"""
_ELEMENTS_JS = """() => {
  const hidden = (el) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || node.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') return true;
    }
    return !el.getClientRects().length;
  };
  const semantic = (el) => {
    const tag = el.tagName.toLowerCase();
    return el.matches('a,button,input,textarea,select,[role],[contenteditable="true"],h1,h2,h3,main,nav,form,li');
  };
  const textFor = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        for (let p = node.parentElement; p && p !== root.parentElement; p = p.parentElement) {
          const style = getComputedStyle(p);
          if (p.matches('input,textarea,select,option,script,style,noscript,[contenteditable="true"]') || p.hidden || p.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    let value = '';
    let node;
    let visited = 0;
    while ((node = walker.nextNode()) && value.length < 400 && visited < 200) { visited += 1; value += ` ${node.nodeValue || ''}`; }
    return value.trim().replace(/\\s+/g, ' ').slice(0, 400);
  };
  const found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let visited = 0;
  let el = walker.currentNode;
  while (el && visited < 2000 && found.length < 80) {
    visited += 1;
    const tag = el.tagName.toLowerCase();
    if (!hidden(el) && (tag !== 'input' || !['password', 'hidden', 'file'].includes((el.getAttribute('type') || '').toLowerCase()))) {
      const text = ['input','textarea','select'].includes(tag) ? '' : textFor(el);
      const parentText = el.parentElement ? textFor(el.parentElement) : '';
      if (semantic(el) || (text && text !== parentText && text.length <= 400)) found.push(el);
    }
    el = walker.nextNode();
  }
  return found;
}"""
_SUMMARY_JS = """el => {
  const key = '__conductor_agent_browser_nodes';
  if (!window[key]) Object.defineProperty(window, key, {value: {ids: new WeakMap(), next: 0}});
  const state = window[key];
  let id = state.ids.get(el);
  if (!id) { id = String(++state.next); state.ids.set(el, id); }
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const implicit = {a:'link',button:'button',textarea:'textbox',select:'combobox',h1:'heading',h2:'heading',h3:'heading',main:'main',nav:'navigation',li:'listitem'};
  if (tag === 'input') implicit.input = ({checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',search:'searchbox'}[type] || 'textbox');
  const role = el.getAttribute('role') || implicit[tag] || (el.isContentEditable ? 'textbox' : 'generic');
  const textFor = (root, limit) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        for (let p = node.parentElement; p && p !== root.parentElement; p = p.parentElement) {
          const style = getComputedStyle(p);
          if (p.matches('input,textarea,select,option,script,style,noscript,[contenteditable="true"]') || p.hidden || p.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    let value = '';
    let node;
    let visited = 0;
    while ((node = walker.nextNode()) && value.length < limit && visited < 200) { visited += 1; value += ` ${node.nodeValue || ''}`; }
    return value.trim().replace(/\\s+/g, ' ').slice(0, limit);
  };
  let label = el.getAttribute('aria-label') || '';
  if (!label && el.getAttribute('aria-labelledby')) label = el.getAttribute('aria-labelledby').split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean).map(node => textFor(node, 180)).join(' ');
  if (!label && el.labels) label = Array.from(el.labels).map(node => textFor(node, 180)).join(' ');
  if (!label && el.id) {
    let associated = null;
    try { associated = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); } catch {}
    if (associated) label = textFor(associated, 180);
  }
  if (!label) label = el.getAttribute('alt') || el.getAttribute('title') || '';
  const control = ['input','textarea','select'].includes(tag);
  const text = control ? '' : textFor(el, 180);
  const name = (label || text).slice(0, 180);
  let parent = el.parentElement;
  while (parent && parent !== document.body && !parent.matches('a,button,input,textarea,select,[role],[contenteditable="true"],h1,h2,h3,main,nav,form,li')) parent = parent.parentElement;
  const parentId = parent && parent !== el ? state.ids.get(parent) || null : null;
  let depth = 0;
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) if (p.matches('a,button,input,textarea,select,[role],[contenteditable="true"],h1,h2,h3,main,nav,form,li')) depth += 1;
  return {node_id:id, parent_node_id:parentId, depth, summary:{role, accessible_name:name, text, tag}};
}"""


def _display_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return ""
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            return ""
        netloc = host if port is None else f"{host}:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path[:512], "", ""))[:1024]
    except ValueError:
        return ""


def _browser_env(profile: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"}}
    environment["HOME"] = str(Path.home())
    environment["TMPDIR"] = str(Path(os.environ.get("TMPDIR", "/tmp")))
    return environment


class PlaywrightDriver:
    """Own one externally started Chromium and connect only through its CDP port."""

    def __init__(
        self,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        startup_timeout: float = 15.0,
    ) -> None:
        self.playwright_factory = playwright_factory
        self.popen_factory = popen_factory
        self.startup_timeout = startup_timeout
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._process: Any = None
        self._profile: Path | None = None
        self._refs = ReferenceRegistry()
        self._frames: dict[str, Any] = {}
        self._page: Any = None

    async def start(self, profile: Path) -> None:
        self._profile = profile
        user_data = profile / "chromium"
        user_data.mkdir(mode=0o700, exist_ok=True)
        metadata = user_data.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise BrowserCommandError("Chromium profile directory must be private")
        active_port = user_data / "DevToolsActivePort"
        try:
            metadata = active_port.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise BrowserCommandError("Refusing an unexpected Chromium debug endpoint file")
            active_port.unlink()
        if self.playwright_factory is None:
            from playwright.async_api import async_playwright

            factory = async_playwright
        else:
            factory = self.playwright_factory
        self._playwright = await factory().start()
        executable = Path(self._playwright.chromium.executable_path)
        if not executable.is_file():
            await self._playwright.stop()
            self._playwright = None
            raise BrowserCommandError("Managed Chromium is not installed; install the Playwright Chromium runtime")
        arguments = [
            str(executable),
            f"--user-data-dir={user_data}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--disable-default-apps",
            "--headless=new",
            "about:blank",
        ]
        try:
            self._process = self.popen_factory(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                umask=0o077,
                env=_browser_env(profile),
            )
            endpoint = await self._wait_for_endpoint(active_port)
            self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=10_000)
            contexts = self._browser.contexts
            if not contexts:
                raise BrowserCommandError("Managed Chromium did not expose its persistent context")
            self._context = contexts[0]
            self._page = self._context.pages[-1] if self._context.pages else await self._context.new_page()
            self._context.on("page", self._on_page)
        except BaseException:
            await self._terminate_child()
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            raise

    async def _wait_for_endpoint(self, path: Path) -> str:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise BrowserCommandError("Managed Chromium exited during startup")
            try:
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                    raise BrowserCommandError("Chromium debug endpoint file is not private")
                lines = path.read_text(encoding="ascii").splitlines()
                if len(lines) != 2 or not lines[0].isdigit() or not lines[1].startswith("/devtools/browser/"):
                    raise BrowserCommandError("Chromium created an invalid debug endpoint")
                port = int(lines[0])
                if not 1 <= port <= 65535 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-" for ch in lines[1]):
                    raise BrowserCommandError("Chromium created an invalid debug endpoint")
                return f"ws://127.0.0.1:{port}{lines[1]}"
            except FileNotFoundError:
                pass
            await asyncio.sleep(0.05)
        raise BrowserCommandError("Managed Chromium did not publish a debug endpoint")

    def _on_page(self, page: Any) -> None:
        self._page = page

    def _current_page(self) -> Any:
        if self._context is None:
            raise BrowserCommandError("Managed browser is not ready")
        pages = self._context.pages
        if self._page is None or self._page.is_closed():
            if pages:
                self._page = pages[-1]
            else:
                self._page = None
        if self._page is None and pages:
            self._page = pages[-1]
        if self._page is None:
            raise BrowserCommandError("Managed browser has no active page")
        return self._page

    async def goto(self, url: str) -> dict[str, Any]:
        await self._refs.clear()
        self._frames.clear()
        page = self._current_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=12_000)
        return {"url": _display_url(page.url), "title": (await page.title())[:200]}

    async def snapshot(self) -> dict[str, Any]:
        page = self._current_page()
        frames = page.frames[:MAX_FRAMES]
        self._refs.retain_frames(frames)
        rows: list[dict[str, Any]] = []
        frame_rows: list[dict[str, Any]] = []
        row_groups: dict[str, list[dict[str, Any]]] = {}
        self._frames = {}
        remaining = MAX_ELEMENTS
        per_frame_limit = max(1, MAX_ELEMENTS // max(1, len(frames)))
        for frame in frames:
            frame_id = self._refs.frame_id(frame)
            self._frames[frame_id] = frame
            frame_url = _display_url(frame.url)
            row_start = len(rows)
            elements = None
            properties: dict[str, Any] = {}
            retained_properties: set[str] = set()
            try:
                document_token = await frame.evaluate(_DOC_TOKEN_JS)
                generation = self._refs.document_generation(frame_id, document_token)
                elements = await frame.evaluate_handle(_ELEMENTS_JS)
                properties = await elements.get_properties()
                item_rows: list[dict[str, Any]] = []
                frame_count = 0
                for key in sorted((key for key in properties if key.isdigit()), key=int):
                    if remaining <= 0 or frame_count >= per_frame_limit:
                        break
                    handle = properties[key].as_element()
                    if handle is None:
                        continue
                    try:
                        semantic = await handle.evaluate(_SUMMARY_JS)
                    except Exception:
                        await handle.dispose()
                        continue
                    remaining -= 1
                    row = {
                        "frame_id": frame_id,
                        "document_generation": generation,
                        "node_id": semantic["node_id"],
                        "parent_node_id": semantic.get("parent_node_id"),
                        "depth": semantic.get("depth", 0),
                        "handle": handle,
                        "resources": (properties[key],),
                        "summary": semantic["summary"],
                    }
                    rows.append(row)
                    item_rows.append(row)
                    retained_properties.add(key)
                    frame_count += 1
                row_groups[frame_id] = item_rows
                frame_rows.append({
                    "frame": frame_id,
                    "url": frame_url,
                    "origin": _origin(frame.url),
                    "status": "ok",
                })
            except Exception:
                for row in rows[row_start:]:
                    try:
                        await row["handle"].dispose()
                    except Exception:
                        pass
                del rows[row_start:]
                frame_rows.append({"frame": frame_id, "url": frame_url, "origin": _origin(frame.url), "status": "unavailable"})
                retained_properties.clear()
            finally:
                for key, property_handle in properties.items():
                    if key not in retained_properties:
                        try:
                            await property_handle.dispose()
                        except Exception:
                            pass
                if elements is not None:
                    try:
                        await elements.dispose()
                    except Exception:
                        pass
        rendered = await self._refs.replace_snapshot(rows)
        by_key = {
            (row["frame_id"], row["document_generation"], row["node_id"]): item
            for row, item in zip(rows, rendered, strict=True)
        }
        frame_result = []
        for frame_info in frame_rows:
            frame_id = frame_info["frame"]
            nodes: dict[str, dict[str, Any]] = {}
            for row in row_groups.get(frame_id, []):
                key = (row["frame_id"], row["document_generation"], row["node_id"])
                snapshot_item = by_key[key]
                node = {field: value for field, value in snapshot_item.items() if field != "parent_node_id"}
                node["depth"] = row.get("depth", 0)
                node["children"] = []
                nodes[snapshot_item["ref"]] = node
            roots: list[dict[str, Any]] = []
            for row in row_groups.get(frame_id, []):
                key = (row["frame_id"], row["document_generation"], row["node_id"])
                snapshot_item = by_key[key]
                node = nodes[snapshot_item["ref"]]
                parent_node_id = row.get("parent_node_id")
                parent_key = (frame_id, row["document_generation"], parent_node_id)
                parent = by_key.get(parent_key) if parent_node_id else None
                parent_node = nodes.get(parent["ref"]) if parent is not None else None
                (parent_node["children"] if parent_node is not None else roots).append(node)
            frame_result.append({**frame_info, "items": roots})
        return {"url": _display_url(page.url), "title": (await page.title())[:200], "frames": frame_result}

    async def evaluate_page(self, script: str) -> dict[str, Any]:
        page = self._current_page()
        # The evaluated value stays inside Chromium; only coarse metadata crosses
        # the protocol boundary, where arbitrary page text could expose secrets.
        metadata = await page.evaluate(
            """async script => {
              const value = await (0, eval)(script);
              let kind = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
              let size = 0;
              try {
                if (typeof value === 'string') size = value.length;
                else if (value !== undefined) size = JSON.stringify(value).length;
              } catch {}
              return {result_kind: kind, result_size: Math.min(size, 1000000)};
            }""",
            script,
        )
        if not isinstance(metadata, dict):
            return {"result_kind": "unknown", "result_size": 0}
        kind = metadata.get("result_kind")
        size = metadata.get("result_size")
        return {
            "result_kind": kind if kind in {"undefined", "object", "boolean", "number", "string", "function", "symbol", "null", "array"} else "unknown",
            "result_size": size if type(size) is int and 0 <= size <= 1_000_000 else 0,
        }

    async def capture_network(self, url: str) -> list[dict[str, Any]]:
        page = self._current_page()
        rows: list[dict[str, Any]] = []

        def on_response(response: Any) -> None:
            if len(rows) >= 51:
                return
            request = response.request
            rows.append({
                "method": str(request.method),
                "origin": _origin(response.url),
                "resource_type": str(request.resource_type),
                "status": int(response.status),
            })

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=12_000)
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        return rows

    async def _terminate_child(self) -> None:
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.to_thread(process.wait, timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            await asyncio.to_thread(process.wait, timeout=3)

    def _frame_for(self, frame_id: str | None) -> Any:
        if frame_id is None:
            return self._current_page().main_frame
        frame = self._frames.get(frame_id)
        if frame is None or frame not in self._current_page().frames:
            raise StaleReferenceError("Frame is stale; take a fresh snapshot")
        return frame

    async def _target(self, role: str, name: str, frame: str | None) -> Any:
        locator = self._frame_for(frame).get_by_role(role, name=name, exact=True)
        count = await locator.count()
        if count != 1:
            raise BrowserCommandError("Semantic target must match exactly one element")
        return await locator.element_handle()

    async def _resolve_ref(self, ref: str) -> Any:
        expected = self._refs.entry_for(ref)
        if expected is None:
            raise StaleReferenceError("Reference is stale; take a fresh snapshot")
        handle = await self._refs.resolve(ref)
        try:
            frame = await handle.owner_frame()
            page = self._current_page()
            if frame is None or frame not in page.frames or self._frames.get(expected[0]) is not frame:
                raise StaleReferenceError("Reference is stale; take a fresh snapshot")
            document_token = await frame.evaluate(_DOC_TOKEN_JS)
            generation = self._refs.document_generation(expected[0], document_token)
            if generation != expected[1]:
                raise StaleReferenceError("Reference is stale; take a fresh snapshot")
        except StaleReferenceError:
            raise
        except Exception:
            raise StaleReferenceError("Reference is stale; take a fresh snapshot") from None
        return handle

    async def click(self, ref: str) -> dict[str, Any]:
        handle = await self._resolve_ref(ref)
        await handle.click(timeout=8_000)
        return {"clicked": True}

    async def click_target(self, role: str, name: str, frame: str | None = None) -> dict[str, Any]:
        handle = await self._target(role, name, frame)
        if handle is None:
            raise BrowserCommandError("Semantic target is no longer available")
        try:
            await handle.click(timeout=8_000)
        finally:
            await handle.dispose()
        return {"clicked": True, "target": {"role": role, "name": name}}

    async def _fill_handle(self, handle: Any, value: str, target: dict[str, str] | None = None) -> dict[str, Any]:
        allowed = await handle.evaluate("el => ['INPUT','TEXTAREA'].includes(el.tagName) && !['password','hidden','file'].includes((el.getAttribute('type') || '').toLowerCase()) || el.isContentEditable")
        if not allowed:
            raise BrowserCommandError("fill target must be a visible non-password text field")
        await handle.fill(value, timeout=8_000)
        return {"filled": True, **({"target": target} if target else {})}

    async def fill(self, ref: str, value: str) -> dict[str, Any]:
        return await self._fill_handle(await self._resolve_ref(ref), value)

    async def fill_target(self, role: str, name: str, value: str, frame: str | None = None) -> dict[str, Any]:
        handle = await self._target(role, name, frame)
        if handle is None:
            raise BrowserCommandError("Semantic target is no longer available")
        try:
            return await self._fill_handle(handle, value, {"role": role, "name": name})
        finally:
            await handle.dispose()

    async def wait(self, seconds: float) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return {"waited_seconds": seconds}

    async def _extract_handle(self, handle: Any, fields: list[str]) -> dict[str, Any]:
        values = await handle.evaluate("""(el, fields) => {
          const tag = el.tagName.toLowerCase();
          const type = (el.getAttribute('type') || '').toLowerCase();
          const implicit = {a:'link',button:'button',textarea:'textbox',select:'combobox',h1:'heading',h2:'heading',h3:'heading',main:'main',nav:'navigation',li:'listitem'};
          if (tag === 'input') implicit.input = ({checkbox:'checkbox',radio:'radio',button:'button',submit:'button',reset:'button',search:'searchbox'}[type] || 'textbox');
          const role = el.getAttribute('role') || implicit[tag] || (el.isContentEditable ? 'textbox' : 'generic');
          const textFor = (root, limit) => {
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
              acceptNode(node) {
                for (let p = node.parentElement; p && p !== root.parentElement; p = p.parentElement) {
                  const style = getComputedStyle(p);
                  if (p.matches('input,textarea,select,option,script,style,noscript,[contenteditable="true"]') || p.hidden || p.getAttribute('aria-hidden') === 'true' || style.display === 'none' || style.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
                }
                return NodeFilter.FILTER_ACCEPT;
              }
            });
            let value = '';
            let node;
            let visited = 0;
            while ((node = walker.nextNode()) && value.length < limit && visited < 200) { visited += 1; value += ` ${node.nodeValue || ''}`; }
            return value.trim().replace(/\\s+/g,' ').slice(0,limit);
          };
          let label = el.getAttribute('aria-label') || '';
          if (!label && el.getAttribute('aria-labelledby')) label = el.getAttribute('aria-labelledby').split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean).map(node => textFor(node,180)).join(' ');
          if (!label && el.labels) label = Array.from(el.labels).map(node => textFor(node,180)).join(' ');
          if (!label && el.id) { let associated = null; try { associated = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); } catch {} if (associated) label = textFor(associated,180); }
          if (!label) label = el.getAttribute('alt') || el.getAttribute('title') || '';
          const text = ['input','textarea','select'].includes(tag) ? '' : textFor(el,1000);
          const result = {};
          for (const field of fields) {
            if (field === 'text') result.text = text;
            else if (field === 'tag') result.tag = tag;
            else if (field === 'role') result.role = role;
            else if (field === 'accessible_name') result.accessible_name = (label || text.slice(0,180)).slice(0,180);
            else if (field === 'title') result.title = (el.getAttribute('title') || '').slice(0,300);
            else if (field === 'href') {
              const raw = el.href || '';
              try {
                let u = new URL(raw);
                const host = u.hostname.toLowerCase();
                if ((host === 'duckduckgo.com' || host.endsWith('.duckduckgo.com')) && u.pathname.startsWith('/l/')) {
                  const target = u.searchParams.get('uddg');
                  if (target) {
                    const decoded = new URL(target);
                    if (decoded.protocol === 'http:' || decoded.protocol === 'https:') u = decoded;
                  }
                }
                result.href = u.origin + u.pathname.slice(0,512);
              } catch { result.href = ''; }
            }
          }
          return result;
        }""", fields)
        return values

    async def extract(self, ref: str, fields: list[str]) -> dict[str, Any]:
        return await self._extract_handle(await self._resolve_ref(ref), fields)

    async def extract_target(self, role: str, name: str, fields: list[str], frame: str | None = None) -> dict[str, Any]:
        handle = await self._target(role, name, frame)
        if handle is None:
            raise BrowserCommandError("Semantic target is no longer available")
        try:
            return await self._extract_handle(handle, fields)
        finally:
            await handle.dispose()

    async def import_storage_state(self, path: Path, domains: list[str]) -> dict[str, Any]:
        state = read_storage_state(path, domains)
        if state.cookies:
            await self._context.add_cookies(state.cookies)
        for origin, entries in state.local_storage.items():
            if not entries:
                continue
            page = await self._context.new_page()
            script_data = json.dumps({"origin": origin, "entries": entries}, ensure_ascii=False)
            script = """(() => {
              const state = JSON.parse(%s);
              if (location.origin === state.origin) {
                for (const [key, value] of Object.entries(state.entries)) localStorage.setItem(key, value);
              }
            })();""" % json.dumps(script_data)

            async def fulfill(route: Any) -> None:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<!doctype html><title>Storage import</title>")

            session = None
            try:
                await page.route("**/*", fulfill)
                await page.add_init_script(script)
                if hasattr(self._context, "new_cdp_session"):
                    session = await self._context.new_cdp_session(page)
                    await session.send("Network.enable")
                    await session.send("Network.setBypassServiceWorker", {"bypass": True})
                await page.goto(origin, wait_until="domcontentloaded", timeout=10_000)
            finally:
                if session is not None:
                    try:
                        await session.detach()
                    except Exception:
                        pass
                await page.close()
        await self._refs.clear()
        return {
            "status": "imported",
            "cookies": len(state.cookies),
            "local_storage_items": sum(len(items) for items in state.local_storage.values()),
            "domains": state.domains,
        }

    async def close(self) -> None:
        await self._refs.clear()
        self._frames.clear()
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        await self._terminate_child()
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return ""
        port = parsed.port
        return f"{parsed.scheme}://{parsed.hostname}{f':{port}' if port is not None else ''}"
    except ValueError:
        return ""
