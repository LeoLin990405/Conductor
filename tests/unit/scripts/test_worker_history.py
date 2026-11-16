"""scripts/worker_history.py: read-only, redacted, capped history (N29 acceptance 8)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.tools.auto_dispatch import context_budget as cb

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "worker_history.py"
KEY = "line-key-1"
RUNS_ROOT_ENV = "CONDUCTOR_HISTORY_RUNS_ROOT"

SECRETS = (
    "ghp_" "abcdefghijklmnopqrstuvwxyz0123456789AB",
    "sk-" "proj-abcdefghijklmnopqrstuvwxyz012345",
    "Bearer abcdef1234567890abcdef1234567890",
    "-----BEGIN " "PRIVATE KEY-----",
)
PEM = (
    "-----BEGIN " "PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7\n"
    "-----END PRIVATE KEY-----\n"
)
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"


@dataclass
class Lineage:
    first: str
    previous: str | None
    current: str
    generation: int


@dataclass
class Fixture:
    runs_dir: Path
    manifest: Path
    logs: dict[str, Path]

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.runs_dir.parent / "home"),
            "LANG": "en_US.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            cb.ENV_HISTORY_MANIFEST: str(self.manifest),
            RUNS_ROOT_ENV: str(self.runs_dir),
        }
        env.update(extra)
        return env


def _entry(runs_dir: Path, run_id: str, *, generation: int, lineage: str, **kw) -> cb.ManifestEntry:
    fields = dict(
        run_id=run_id,
        lineage_current=lineage,
        session_generation=generation,
        provider="grok",
        log_path=str(runs_dir / f"{run_id}.log"),
        started_at="2026-11-14T00:00:00Z",
        ended_at="2026-11-14T00:01:00Z",
        exit_code=0,
        handoff_path=None,
        estimate_source="log_bytes",
    )
    fields.update(kw)
    return cb.ManifestEntry(**fields)


@pytest.fixture
def hist(tmp_path: Path) -> Fixture:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (tmp_path / "home").mkdir()
    manifest = cb.manifest_path(runs_dir, KEY)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    logs = {
        "r1": runs_dir / "r1.log",
        "r2": runs_dir / "r2.log",
        "r3": runs_dir / "r3.log",
    }
    words = " ".join(f"w{index:05d}" for index in range(6000)) + "\n"  # ~42 KB
    logs["r1"].write_text(words, encoding="utf-8")
    logs["r2"].write_text(
        "start\n"
        + f"token {SECRETS[0]} end\n"
        + f"key {SECRETS[1]} end\n"
        + f"Authorization: {SECRETS[2]}\n"
        + PEM
        + "needle a.b*c(d) here\n" * 30
        + "done\n",
        encoding="utf-8",
    )
    logs["r3"].write_bytes(b"binary \xff\xfe tail\n")
    note = runs_dir / "recovery-sessions" / "handoff-g1.md"
    note.write_text("handoff note body for generation one\n", encoding="utf-8")
    cb.append_manifest(
        manifest, _entry(runs_dir, "r1", generation=1, lineage="aaa"), runs_dir=runs_dir
    )
    cb.append_manifest(
        manifest,
        _entry(
            runs_dir,
            "r2",
            generation=1,
            lineage="aaa",
            handoff_path=str(note),
            handoff_request=cb.HANDOFF_OK,
        ),
        runs_dir=runs_dir,
    )
    cb.append_manifest(
        manifest,
        _entry(runs_dir, "r3", generation=2, lineage="bbb", exit_code=1),
        runs_dir=runs_dir,
    )
    return Fixture(runs_dir=runs_dir, manifest=manifest, logs=logs)


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    state: dict[str, tuple[str, int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            digest = ""
            if path.is_file() and not path.is_symlink():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            state[str(path.relative_to(root))] = (digest, info.st_mtime_ns, info.st_mode)
    return state


def _run(fixture: Fixture, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    before = _snapshot(fixture.runs_dir)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        env=fixture.env() if env is None else env,
        cwd=str(cwd or fixture.runs_dir.parent),
        timeout=60,
        check=False,
    )
    assert _snapshot(fixture.runs_dir) == before, "runs_dir changed"
    return proc


def _out(proc) -> str:
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout) <= 64 * 1024
    return proc.stdout.decode("utf-8")


def _read_all(fixture: Fixture, run_id: str, limit: int) -> Iterator[tuple[str, str]]:
    offset = 0
    while True:
        text = _out(_run(fixture, "read", "--run", run_id, "--offset", str(offset), "--limit", str(limit)))
        header, _, content = text.partition("\n")
        yield header, content
        next_offset = header.rsplit("next_offset=", 1)[1]
        if next_offset == "eof":
            return
        offset = int(next_offset)


# ---------------------------------------------------------------------------
# list


def test_list_groups_generations_without_content(hist: Fixture) -> None:
    text = _out(_run(hist, "list"))
    assert text.startswith(cb.HISTORY_DATA_MARKER)
    assert "generation=1 lineage_current=aaa provider=grok runs=2" in text
    assert "handoff_request=none,ok handoff_notes=1" in text
    assert "generation=2 lineage_current=bbb provider=grok runs=1" in text
    r1 = hist.logs["r1"].stat().st_size
    assert f"run=r1 bytes={r1} exit_code=0" in text
    assert "run=r3 bytes=" in text and "exit_code=1" in text
    assert "w00001" not in text and "needle" not in text


def test_runs_from_any_cwd(hist: Fixture, tmp_path: Path) -> None:
    elsewhere = tmp_path / "worktree"
    elsewhere.mkdir()
    assert "generation=1" in _out(_run(hist, "list", cwd=elsewhere))


# ---------------------------------------------------------------------------
# read


def test_read_default_page_is_8k_and_marked(hist: Fixture) -> None:
    text = _out(_run(hist, "read", "--run", "r1"))
    header, _, content = text.partition("\n")
    assert header.startswith(cb.HISTORY_DATA_MARKER + " run=r1 offset=0 ")
    end = int(header.split(" end=")[1].split()[0])
    assert 8192 - 512 <= end <= 8192
    assert content.rstrip("\n") == hist.logs["r1"].read_text()[:end].rstrip("\n")


def test_read_limit_clamped_to_16k(hist: Fixture) -> None:
    text = _out(_run(hist, "read", "--run", "r1", "--limit", "1000000"))
    header, _, content = text.partition("\n")
    end = int(header.split(" end=")[1].split()[0])
    assert 16384 - 512 <= end <= 16384
    assert len(content.encode()) <= 16384 + 1


@pytest.mark.parametrize("limit", [4096, 16384])
def test_read_pages_reassemble_the_log(hist: Fixture, limit: int) -> None:
    pages = list(_read_all(hist, "r1", limit))
    original = hist.logs["r1"].read_text()
    assert len(pages) >= len(original) // limit
    # Page edges sit on whitespace, so no word is split between two pages.
    tokens = [token for _, content in pages for token in content.split()]
    assert tokens == original.split()
    previous_end = 0
    for header, _ in pages:
        start = int(header.split(" offset=")[1].split()[0])
        end = int(header.split(" end=")[1].split()[0])
        assert start == previous_end
        assert 0 < end - start <= limit
        previous_end = end
    assert previous_end == len(original.encode())


def test_read_offset_at_and_past_eof(hist: Fixture) -> None:
    size = hist.logs["r1"].stat().st_size
    for offset in (size, size + 100):
        header = _out(_run(hist, "read", "--run", "r1", "--offset", str(offset))).partition("\n")[0]
        assert "next_offset=eof" in header
        assert f"size={size}" in header


def test_read_offset_inside_word_moves_to_whitespace(hist: Fixture) -> None:
    header, _, content = _out(_run(hist, "read", "--run", "r1", "--offset", "3", "--limit", "100")).partition("\n")
    assert " offset=7 " in header  # "w00000 " is 7 bytes
    assert content.startswith("w00001")


def test_read_handoff_note(hist: Fixture) -> None:
    text = _out(_run(hist, "read", "--handoff", "1"))
    assert "handoff generation=1" in text
    assert "handoff note body for generation one" in text
    proc = _run(hist, "read", "--handoff", "2")
    assert proc.returncode != 0
    assert proc.stderr.decode().strip() == "error: unknown_handoff"


def test_read_unknown_run_fails_closed(hist: Fixture) -> None:
    proc = _run(hist, "read", "--run", "nope")
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert proc.stderr.decode().strip() == "error: unknown_run"


def test_read_binary_is_decoded_with_replacement(hist: Fixture) -> None:
    text = _out(_run(hist, "read", "--run", "r3"))
    assert "binary" in text and "�" in text


# ---------------------------------------------------------------------------
# redaction


def test_secrets_redacted_in_read_and_search(hist: Fixture) -> None:
    outputs = [
        _out(_run(hist, "read", "--run", "r2", "--limit", "16384")),
        _out(_run(hist, "search", "--query", "end")),
        _out(_run(hist, "search", "--query", "PRIVATE")),
        _out(_run(hist, "search", "--query", "ghp_")),
    ]
    for text in outputs:
        for secret in SECRETS:
            assert secret.split()[-1] not in text
        assert PEM_BODY not in text
    assert "[redacted]" in outputs[0]


def test_page_starting_inside_private_key_block_is_redacted(hist: Fixture) -> None:
    raw = hist.logs["r2"].read_bytes()
    offset = raw.index(PEM_BODY.encode())
    text = _out(_run(hist, "read", "--run", "r2", "--offset", str(offset), "--limit", "200"))
    assert f" offset={offset} " in text
    assert PEM_BODY[:16] not in text


# ---------------------------------------------------------------------------
# search


def test_search_is_literal_with_regex_metacharacters(hist: Fixture) -> None:
    text = _out(_run(hist, "search", "--query", "a.b*c(d)"))
    assert "search hits=20 max=20" in text
    assert text.count(cb.HISTORY_DATA_MARKER + " hit=") == 20
    # As a regex this would match "aXbbbc(d)"-like text; literal finds nothing.
    assert "search hits=0" in _out(_run(hist, "search", "--query", "a.*here"))
    assert "search hits=0" in _out(_run(hist, "search", "--query", "(((("))


def test_search_max_clamped_and_respected(hist: Fixture) -> None:
    assert "search hits=20 max=20" in _out(_run(hist, "search", "--query", "needle", "--max", "500"))
    text = _out(_run(hist, "search", "--query", "needle", "--max", "3"))
    assert "search hits=3 max=3" in text
    assert text.count(" hit=") == 3


def test_search_context_is_about_200_bytes(hist: Fixture) -> None:
    text = _out(_run(hist, "search", "--query", "w03000", "--max", "1"))
    header, _, rest = text.partition(" hit=1 ")
    content = rest.partition("\n")[2]
    assert "w03000" in content
    assert 400 <= len(content.strip()) <= 400 + 6 + 2 * 512
    assert "run=r1 kind=log" in rest


def test_search_covers_handoff_notes(hist: Fixture) -> None:
    text = _out(_run(hist, "search", "--query", "generation one"))
    assert "kind=handoff" in text


# ---------------------------------------------------------------------------
# budget


def _budget_env(hist: Fixture, provider: str = "grok") -> tuple[dict[str, str], Path]:
    est = cb.estimate(provider, prior_log_bytes=4000, current_run_log_bytes=0, env={})
    block = cb.render_budget_block(
        est, Lineage("aaa", None, "aaa", 1), soft_ratio=cb.SOFT_RATIO_DEFAULT
    )
    budget_dir = hist.runs_dir / cb.RECOVERY_DIR / cb.BUDGET_SUBDIR
    budget_dir.mkdir(parents=True, exist_ok=True)
    budget_file = budget_dir / "r9.txt"
    cb.write_budget_file(budget_file, block)
    run_log = hist.runs_dir / "r9.log"
    env = hist.env(
        **{cb.ENV_BUDGET_FILE: str(budget_file), cb.ENV_RUN_LOG: str(run_log)}
    )
    return env, run_log


def _field(text: str, name: str) -> str:
    for line in text.splitlines():
        if line.startswith(name + ": "):
            return line.split(": ", 1)[1]
    raise AssertionError(name)


def test_budget_tokens_left_decreases_as_log_grows(hist: Fixture) -> None:
    env, run_log = _budget_env(hist)
    first = _out(_run(hist, "budget", env=env))  # run log not created yet
    assert _field(first, "source") == "log_bytes"
    assert _field(first, "soft_ratio") == str(cb.SOFT_RATIO_DEFAULT)
    assert _field(first, "soft_ratio_reached") == "false"
    left0 = int(_field(first, "tokens_left"))
    run_log.write_bytes(b"x" * 40_000)
    left1 = int(_field(_out(_run(hist, "budget", env=env)), "tokens_left"))
    assert left1 == left0 - 10_000
    run_log.write_bytes(b"x" * 80_000)
    left2 = int(_field(_out(_run(hist, "budget", env=env)), "tokens_left"))
    assert left2 == left1 - 10_000


def test_budget_soft_ratio_reached(hist: Fixture) -> None:
    env, run_log = _budget_env(hist)
    window = int(_field(_out(_run(hist, "budget", env=env)), "window_tokens"))
    with run_log.open("wb") as handle:
        handle.truncate(window * cb.BYTES_PER_TOKEN)  # sparse file
    text = _out(_run(hist, "budget", env=env))
    assert _field(text, "soft_ratio_reached") == "true"
    assert _field(text, "tokens_left") == "0"


def test_budget_without_baseline_is_unavailable(hist: Fixture) -> None:
    for env in (
        hist.env(),
        hist.env(**{cb.ENV_BUDGET_FILE: str(hist.runs_dir / "recovery-sessions" / "nope.txt")}),
    ):
        proc = _run(hist, "budget", env=env)
        assert proc.returncode != 0
        assert proc.stderr.decode().strip() == "error: budget_unavailable"
        assert "tokens_left: unknown" in proc.stdout.decode()


def test_budget_malformed_block_is_unavailable(hist: Fixture) -> None:
    bad = hist.runs_dir / "recovery-sessions" / "bad.txt"
    bad.write_text("not a budget block\n")
    proc = _run(hist, "budget", env=hist.env(**{cb.ENV_BUDGET_FILE: str(bad)}))
    assert proc.returncode != 0
    assert proc.stderr.decode().strip() == "error: budget_unavailable"


def test_budget_file_outside_runs_dir_refused(hist: Fixture, tmp_path: Path) -> None:
    env, _ = _budget_env(hist)
    outside = tmp_path / "outside.txt"
    outside.write_text((hist.runs_dir / cb.RECOVERY_DIR / cb.BUDGET_SUBDIR / "r9.txt").read_text())
    env[cb.ENV_BUDGET_FILE] = str(outside)
    proc = _run(hist, "budget", env=env)
    assert proc.returncode == 2
    assert proc.stderr.decode().strip() == "error: path_outside_runs_dir"


def test_budget_symlinked_run_log_refused(hist: Fixture, tmp_path: Path) -> None:
    env, run_log = _budget_env(hist)
    target = tmp_path / "big.log"
    target.write_bytes(b"x" * 100)
    run_log.symlink_to(target)
    proc = _run(hist, "budget", env=env)
    assert proc.returncode == 2
    assert proc.stderr.decode().strip() == "error: path_symlink"


# ---------------------------------------------------------------------------
# fail closed


def _rewrite_manifest(hist: Fixture, **changes: str) -> None:
    lines = hist.manifest.read_text().splitlines()
    record = json.loads(lines[0])
    for key, value in changes.items():
        assert key in record
        record[key] = value
    lines[0] = json.dumps(record)
    hist.manifest.write_text("\n".join(lines) + "\n")


def _refused(proc, code: str) -> None:
    assert proc.returncode != 0
    assert proc.stdout == b"" or code == "budget_unavailable"
    assert proc.stderr.decode().strip() == f"error: {code}"


@pytest.mark.parametrize("command", [["list"], ["read", "--run", "r1"], ["search", "--query", "w0"]])
def test_out_of_bounds_log_path_refused(hist: Fixture, tmp_path: Path, command: list[str]) -> None:
    outside = tmp_path / "secret.log"
    outside.write_text("outside data w0\n")
    _rewrite_manifest(hist, log_path=str(outside))
    _refused(_run(hist, *command), "path_outside_runs_dir")


def test_dotdot_log_path_refused(hist: Fixture, tmp_path: Path) -> None:
    (tmp_path / "secret.log").write_text("outside\n")
    _rewrite_manifest(hist, log_path=str(hist.runs_dir / ".." / "secret.log"))
    _refused(_run(hist, "read", "--run", "r1"), "path_outside_runs_dir")


def test_symlinked_log_refused(hist: Fixture, tmp_path: Path) -> None:
    outside = tmp_path / "secret.log"
    outside.write_text("outside data\n")
    hist.logs["r1"].unlink()
    hist.logs["r1"].symlink_to(outside)
    _refused(_run(hist, "read", "--run", "r1"), "path_symlink")
    _refused(_run(hist, "list"), "path_symlink")


def test_symlinked_parent_directory_refused(hist: Fixture, tmp_path: Path) -> None:
    real_dir = tmp_path / "elsewhere"
    real_dir.mkdir()
    (real_dir / "x.log").write_text("hidden\n")
    (hist.runs_dir / "linkdir").symlink_to(real_dir)
    _rewrite_manifest(hist, log_path=str(hist.runs_dir / "linkdir" / "x.log"))
    _refused(_run(hist, "read", "--run", "r1"), "path_symlink")


def test_symlinked_manifest_refused(hist: Fixture, tmp_path: Path) -> None:
    copy = tmp_path / "manifest-copy.jsonl"
    copy.write_bytes(hist.manifest.read_bytes())
    hist.manifest.unlink()
    hist.manifest.symlink_to(copy)
    for command in (["list"], ["read", "--run", "r1"], ["search", "--query", "w0"], ["budget"]):
        _refused(_run(hist, *command), "manifest_symlink")


def test_missing_manifest_refused(hist: Fixture) -> None:
    hist.manifest.unlink()
    for command in (["list"], ["read", "--run", "r1"], ["search", "--query", "w0"]):
        _refused(_run(hist, *command), "manifest_missing")


def test_manifest_env_unset_refused(hist: Fixture) -> None:
    env = hist.env()
    del env[cb.ENV_HISTORY_MANIFEST]
    for command in (["list"], ["budget"]):
        _refused(_run(hist, *command, env=env), "manifest_unset")


def test_runs_root_unset_refused(hist: Fixture) -> None:
    env = hist.env()
    del env[RUNS_ROOT_ENV]
    for command in (["list"], ["read", "--run", "r1"], ["search", "--query", "w0"], ["budget"]):
        _refused(_run(hist, *command, env=env), "runs_root_unset")


def test_relative_runs_root_refused(hist: Fixture) -> None:
    env = hist.env(**{RUNS_ROOT_ENV: "runs"})
    _refused(_run(hist, "list", env=env), "runs_root_invalid")


def _other_line(tmp_path: Path) -> tuple[Path, Path]:
    """Another line's runs dir with a manifest listing its own (secret) log."""
    other = tmp_path / "other-runs"
    other.mkdir()
    log = other / "o1.log"
    log.write_text("other line secret w0\n")
    manifest = cb.manifest_path(other, "other-line")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    cb.append_manifest(manifest, _entry(other, "o1", generation=1, lineage="ccc"), runs_dir=other)
    return other, manifest


def test_other_lines_manifest_outside_pinned_root_refused(hist: Fixture, tmp_path: Path) -> None:
    _, manifest = _other_line(tmp_path)
    env = hist.env(**{cb.ENV_HISTORY_MANIFEST: str(manifest)})
    for command in (["list"], ["read", "--run", "o1"], ["search", "--query", "w0"], ["budget"]):
        proc = _run(hist, *command, env=env)
        _refused(proc, "manifest_outside_runs_root")
        assert b"secret" not in proc.stdout + proc.stderr


def test_manifest_elsewhere_under_runs_root_refused(hist: Fixture) -> None:
    stray = hist.runs_dir / "recovery-sessions" / "stray.jsonl"
    stray.write_bytes(hist.manifest.read_bytes())
    nested = hist.manifest.parent / "sub"
    nested.mkdir()
    (nested / "x.jsonl").write_bytes(hist.manifest.read_bytes())
    for path in (stray, nested / "x.jsonl", hist.runs_dir / "m.jsonl"):
        env = hist.env(**{cb.ENV_HISTORY_MANIFEST: str(path)})
        _refused(_run(hist, "list", env=env), "manifest_outside_runs_root")


def test_runs_path_with_spaces_works(tmp_path: Path) -> None:
    runs_dir = tmp_path / "a dir" / "runs with space"
    runs_dir.mkdir(parents=True)
    log = runs_dir / "r 1.log"
    log.write_text("hello spaced world\n")
    manifest = cb.manifest_path(runs_dir, KEY)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    cb.append_manifest(
        manifest, _entry(runs_dir, "r1", generation=1, lineage="aaa", log_path=str(log)),
        runs_dir=runs_dir,
    )
    fixture = Fixture(runs_dir=runs_dir, manifest=manifest, logs={"r1": log})
    (runs_dir.parent / "home").mkdir()
    text = _out(_run(fixture, "read", "--run", "r1"))
    assert "hello spaced world" in text


def test_malformed_manifest_refused(hist: Fixture) -> None:
    hist.manifest.write_text("{not json\n")
    _refused(_run(hist, "list"), "manifest_malformed")
    hist.manifest.write_text(json.dumps({"run_id": "r1"}) + "\n")
    _refused(_run(hist, "list"), "manifest_malformed")


def test_usage_error_prints_reason_code_only(hist: Fixture) -> None:
    proc = _run(hist, "read")
    _refused(proc, "usage")
    _refused(_run(hist, "search", "--query", ""), "usage")
    _refused(_run(hist, "read", "--run", "r1", "--offset", "-1"), "usage")
    # There is no option that points at another manifest.
    _refused(_run(hist, "list", "--manifest", str(hist.manifest)), "usage")


def test_search_with_long_words_stays_under_cap(hist: Fixture) -> None:
    hist.logs["r1"].write_text(("Q" + "z" * 4000 + " ") * 40)
    text = _out(_run(hist, "search", "--query", "Q", "--max", "20"))
    assert "search hits=20" in text


def _module():
    spec = importlib.util.spec_from_file_location("worker_history_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_output_capped_at_64k(capsysbinary) -> None:
    module = _module()
    out = module._Output()
    for index in range(10):
        out.segment(f"part={index}", "\u00e9" * 20_000)
    out.flush()
    data = capsysbinary.readouterr().out
    assert len(data) <= module.STDOUT_CAP_BYTES
    assert out.truncated
    data.decode("utf-8")  # no partial UTF-8 sequence at the cut
    assert data.endswith(b"[history output truncated at 64 KiB]\n")
