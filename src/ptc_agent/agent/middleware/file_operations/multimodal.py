"""Write-side half of multimodal support: getting an image, PDF or video into history.

Intercepts a Read of an image/PDF/video path or URL, fetches the bytes, and
hands back the tool result carrying them as content blocks, so the attachment
stays bound to the call that produced it.

Whether the model about to be called can still read those blocks is a separate
question answered on every later turn, in ``multimodal_strip``.

Supported formats:
- Images: PNG, JPG, JPEG, GIF, WebP
- Documents: PDF
- Videos: MP4, MOV (QuickTime), WebM, MKV (Matroska)
"""

import asyncio
import base64
import io
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from PIL import Image

from ptc_agent.agent.tools.file_ops import VIDEO_EXTENSIONS, is_memo_text_path
from src.tools.web.inhouse.guard import BlockedAddressError, GuardedAsyncTransport

logger = logging.getLogger(__name__)

# Supported image extensions
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# Supported document extensions
DOCUMENT_EXTENSIONS = frozenset({".pdf"})

# Combined visual extensions (images, documents and videos)
VISUAL_EXTENSIONS = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS | VIDEO_EXTENSIONS

# PIL reports a format name; providers speak MIME. A format outside this map is
# one no provider accepts, so it is refused rather than given a near-miss guess.
_PIL_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

# ISO base media (MP4/MOV) brands that mean "this file carries video". The brand
# is the only cheap discriminator available: ``ftyp`` alone also opens AVIF and
# HEIF still images, and calling one of those a video sends a block the provider
# rejects on every replay.
_ISO_BMFF_VIDEO_BRANDS = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso4",
        b"iso5",
        b"iso6",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"M4VH",
        b"M4VP",
        b"dash",
        b"msdh",
        b"msix",
        b"mmp4",
        b"f4v ",
        b"F4V ",
    }
)

# EBML header: Matroska and WebM share it, and the DocType inside it (which is
# always near the start) is what separates them.
_EBML_MAGIC = b"\x1aE\xdf\xa3"

# The widest per-request page ceiling any provider we ship publishes (Gemini's).
# Deliberately not the tightest: injection cannot know which model will consume
# the block — the middleware instance is shared with every subagent — so a cap
# picked for the strictest target would refuse documents the actual target
# accepts. What each target can really take is enforced on the read side, where
# the model is known; this bound only rejects what nobody would accept.
MAX_PDF_PAGES = 1000


class _UnusableContent(Exception):
    """Why bytes can't be injected. The text reaches the agent, so it reads as a
    predicate ("not a readable PDF") that slots into the caller's message."""


def _iso_bmff_has_video_track(moov_payload: bytes) -> bool:
    """Whether a ``moov`` payload declares a video track.

    Read off the track handler box rather than by searching for the literal
    ``vide``, which also appears inside movie names and other metadata. A
    handler box is ``size, type, version/flags, pre_defined, handler_type``, so
    the four bytes at ``hdlr`` + 12 are the media type.
    """
    index = moov_payload.find(b"hdlr")
    while index != -1:
        if moov_payload[index + 12 : index + 16] == b"vide":
            return True
        index = moov_payload.find(b"hdlr", index + 4)
    return False


def _detect_video_mime_type(content: bytes) -> str | None:
    """MIME of a structurally valid video container, or None if it is not one.

    Two families are recognised: ISO base media (MP4/MOV, ``ftyp`` box) and
    EBML (WebM/Matroska). Both are verified structurally, not just by magic
    numbers, for the same reason PDFs are: these bytes are checkpointed and
    replayed, so a container-shaped file that no provider can actually decode
    becomes a permanent 400 rather than a one-turn refusal.
    """
    if content.startswith(_EBML_MAGIC):
        head = content[:256]
        if b"webm" in head:
            return "video/webm"
        if b"matroska" in head:
            return "video/x-matroska"
        return None

    if len(content) < 12 or content[4:8] != b"ftyp":
        return None

    # ``ftyp`` body is the major brand followed by compatible brands, four bytes
    # each; the box ends where its own size says it does.
    box_size = int.from_bytes(content[0:4], "big")
    if not 8 <= box_size <= len(content):
        return None
    major_brand = content[8:12]
    brands = {content[i : i + 4] for i in range(8, box_size - 3, 4)}

    # The declared brand only narrows the answer. A truncated download keeps its
    # ``ftyp`` but loses the ``moov`` that carries the sample tables, and the
    # provider rejects that on every replay of the checkpoint — so the movie box
    # and a video track handler are both required, exactly as a decoder would
    # require them.
    mime_type = (
        "video/quicktime"
        if major_brand == b"qt  "
        else "video/mp4"
        if brands & _ISO_BMFF_VIDEO_BRANDS
        else None
    )

    offset = 0
    while offset + 8 <= len(content):
        size = int.from_bytes(content[offset : offset + 4], "big")
        box_header = 8
        if size == 1:  # 64-bit largesize follows the type
            if offset + 16 > len(content):
                return None
            size = int.from_bytes(content[offset + 8 : offset + 16], "big")
            box_header = 16
        elif size == 0:  # box extends to EOF
            size = len(content) - offset
        if size < box_header or offset + size > len(content):
            return None
        if content[offset + 4 : offset + 8] == b"moov":
            payload = content[offset + box_header : offset + size]
            if not _iso_bmff_has_video_track(payload):
                return None
            # An unlisted brand is still a video when the movie box says so:
            # some muxers write brands we do not enumerate.
            return mime_type or "video/mp4"
        offset += size
    return None


def _detect_mime_type(content: bytes) -> tuple[str, int | None]:
    """MIME type and page count, raising `_UnusableContent` if nothing accepts the bytes.

    The name can't decide this: a URL may carry no extension at all, and a
    sandbox path is only as accurate as whatever wrote the file. Both callers
    checkpoint what they inject, so anything a provider would reject has to be
    caught here — otherwise it replays its 400 on every later turn instead of
    failing once. Hence structural verification, not just a magic-byte peek.

    The page count is returned so it can be stamped on the block: the read side
    needs it to judge the PDF against the target's own ceiling, and re-deriving
    it there would mean decoding and reparsing every PDF in history per call.
    """
    if content.startswith(b"%PDF"):
        import pypdf

        try:
            pages = len(pypdf.PdfReader(io.BytesIO(content)).pages)
        except Exception:
            raise _UnusableContent("not a readable PDF") from None
        if pages > MAX_PDF_PAGES:
            raise _UnusableContent(
                f"a {pages}-page PDF, over the {MAX_PDF_PAGES}-page limit"
            )
        return "application/pdf", pages

    # Video containers are checked before PIL, which cannot open them: its
    # failure is indistinguishable from a corrupt image, and the refusal has to
    # name the video case so the agent does not go looking for an image fix.
    video_mime = _detect_video_mime_type(content)
    if video_mime is not None:
        return video_mime, None

    try:
        img = Image.open(io.BytesIO(content))
        img.verify()
    except Exception:
        raise _UnusableContent("not a readable image, PDF or video") from None
    mime_type = _PIL_FORMAT_TO_MIME.get(img.format or "")
    if mime_type is None:
        raise _UnusableContent(f"in {img.format} format, which no provider accepts")
    return mime_type, None


def _is_visual_request(file_path: str) -> bool:
    """Whether a Read of this path or URL should carry an attachment back."""
    # A URL could be an image, a PDF or a video; only the bytes will say.
    if file_path.startswith(("http://", "https://")):
        return True

    # A memo PDF is served as extracted text and has no sandbox-FS copy to
    # fetch, so intercepting it would trade real content for a not-found error.
    if is_memo_text_path(file_path):
        return False

    return Path(file_path).suffix.lower() in VISUAL_EXTENSIONS


def _error(tool_call_id: str, text: str) -> ToolMessage:
    """A refusal the agent can read. The ``ERROR:`` prefix is the tool convention."""
    return ToolMessage(
        content=f"ERROR: {text}", tool_call_id=tool_call_id, status="error"
    )


def build_content_blocks(
    b64_string: str,
    source: str,
    mime_type: str,
    pages: int | None = None,
) -> list[dict[str, Any]]:
    """Content blocks for verified bytes, labelled and ready to ride on a result.

    ``pages`` is stamped on the PDF block for the read-side ceiling check. Every
    provider converter drops keys it doesn't recognise, so it stays a local
    annotation and never reaches the wire.

    ``mime_type`` comes from ``_detect_mime_type``, which admits PDFs, the four
    image formats in ``_PIL_FORMAT_TO_MIME`` and the video containers in
    ``_detect_video_mime_type`` and nothing else.
    """
    if mime_type == "application/pdf":
        # LangChain's file block, which converts to Anthropic's document format.
        filename = Path(source).name
        block: dict[str, Any] = {
            "type": "file",
            "base64": b64_string,
            "mime_type": mime_type,
            "filename": filename,
        }
        if pages is not None:
            block["pages"] = pages
        return [{"type": "text", "text": f"[Viewing PDF: {filename}]"}, block]

    if mime_type.startswith("video/"):
        # OpenAI-compatible video input, which is also the shape StepFun's Chat
        # API documents: a ``video_url`` envelope whose URL may be an http(s)
        # URL or an inline data URI. Inline is what this middleware always
        # writes — providers cannot reach the sandbox, and several cannot fetch
        # external URLs at all.
        return [
            {"type": "text", "text": f"[Viewing video: {Path(source).name}]"},
            {
                "type": "video_url",
                "video_url": {"url": f"data:{mime_type};base64,{b64_string}"},
            },
        ]

    return [
        {"type": "text", "text": "[Viewing image]"},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{b64_string}"},
        },
    ]


def attach_to_tool_result(
    tool_result: ToolMessage,
    content_blocks: list[dict[str, Any]],
) -> ToolMessage:
    """Carry the attachment on the tool result rather than a message after it.

    A separate carrier ``HumanMessage`` landed between the results of a parallel
    batch. That cost two things: a read-time reorder to satisfy Anthropic's rule
    that tool_result blocks open a user turn, and a compaction cutoff that could
    stop on the carrier and strand the rest of the group behind a summarized
    parent. On the tool result the attachment cannot be separated from the call
    that produced it, so neither is reachable.
    """
    ack = tool_result.text
    blocks = list(content_blocks)

    if ack and blocks and blocks[0].get("type") == "text":
        # Folded into the existing label instead of prepended as a second text
        # block, so the result keeps the one-text-plus-attachment shape every
        # route was probed against.
        blocks[0] = {**blocks[0], "text": f"{ack}\n{blocks[0].get('text', '')}"}
    elif ack:
        blocks.insert(0, {"type": "text", "text": ack})

    return tool_result.model_copy(update={"content": blocks})


class MultimodalMiddleware(AgentMiddleware):
    """Intercept Read for images/PDFs/videos and attach the bytes to the tool result.

    Content is always downloaded and base64-encoded rather than passed by URL:
    several providers cannot fetch an external URL at all, and the ones that can
    cannot reach a private bucket.
    """

    # Must track the name file_ops.py registers (``@tool("Read")``) — this is
    # what awrap_tool_call matches on, and a mismatch silently turns the whole
    # middleware into dead code rather than failing anywhere visible.
    TOOL_NAME = "Read"

    # Raw bytes, deliberately below every provider's published ceiling: those are
    # quoted on the base64 string, which is 4/3 larger, so 5 MiB here is ~6.7 MiB
    # on the wire. Both fetch paths write the block into graph state via the tool
    # result, so an oversized block is not a one-turn error: it is checkpointed
    # and replayed on every later turn, and the modality strip cannot save a
    # target that legitimately has the image modality. Refuse before encoding
    # rather than brick the thread.
    MAX_CONTENT_BYTES = 5 * 1024 * 1024

    # Videos get their own ceiling because no provider publishes one that binds
    # the way Anthropic's per-image 5 MiB does. The real constraint is inline
    # size: Gemini's total inline request ceiling is 20 MiB, and 10 MiB of video
    # becomes ~13.3 MiB of base64 with room for the rest of the prompt. StepFun
    # allows 128 MiB per MP4, which is a fetch limit rather than a body limit,
    # so the inline request ceiling is the one that actually refuses. A larger
    # clip needs URL mode (or the provider Files API), not a bigger cap here.
    MAX_VIDEO_BYTES = 10 * 1024 * 1024

    def __init__(self, *, sandbox: Any) -> None:
        """
        Args:
            sandbox: PTCSandbox the Read tool is backed by, used to fetch the
                raw bytes of a path the tool only acknowledged.
        """
        super().__init__()
        self.sandbox = sandbox

    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        tool_call = request.tool_call
        if tool_call.get("name") != self.TOOL_NAME:
            return await handler(request)

        tool_call_id = tool_call.get("id", "unknown")
        file_path = tool_call.get("args", {}).get("file_path", "")
        if not _is_visual_request(file_path):
            return await handler(request)

        # Injected unconditionally, without asking whether the model can view it.
        # This middleware is a single instance shared with every subagent, so the
        # only model it could ask about is the configured one — and gating on that
        # withheld the blocks from a subagent running its own vision model, which
        # the read-side strip cannot undo because it only ever removes blocks. The
        # cost is that a text-only turn still checkpoints content it will strip on
        # every read; that is the accepted price of never silently losing a read.
        logger.debug(f"[MULTIMODAL] Intercepting Read for visual content: {file_path}")

        result = await handler(request)

        # The tool already failed; there are no bytes to go looking for.
        if isinstance(result.content, str) and result.content.startswith("ERROR:"):
            return result

        if file_path.startswith(("http://", "https://")):
            return await self._handle_url_content(file_path, result, tool_call_id)
        return await self._handle_sandbox_content(file_path, result, tool_call_id)

    def _limit_for(self, mime_type: str) -> int:
        """Per-kind ceiling, decided only after the bytes said what they are.

        A path's extension cannot pick this: a URL may have none, and a file
        named ``.mp4`` may really be a 20 MB PNG (or the reverse).
        """
        return (
            self.MAX_VIDEO_BYTES
            if mime_type.startswith("video/")
            else self.MAX_CONTENT_BYTES
        )

    def _fetch_limit(self) -> int:
        """Coarse cap for bytes of unknown kind, before type detection.

        Both fetch paths must allow up to the widest kind so a legitimate video
        is not refused by the image ceiling; the per-kind check happens in
        ``_attach_bytes`` once the bytes have been typed.
        """
        return max(self.MAX_CONTENT_BYTES, self.MAX_VIDEO_BYTES)

    def _too_large(self, source: str, tool_call_id: str, limit: int) -> ToolMessage:
        """Refuse an oversized block with a bare error result.

        The point of refusing is that nothing oversized reaches the checkpoint,
        so the refusal carries no content blocks at all.
        """
        limit_mb = limit // (1024 * 1024)
        logger.warning(
            f"[MULTIMODAL] Content exceeds {limit} bytes, refusing: {source}"
        )
        return _error(
            tool_call_id,
            f"Content is larger than the {limit_mb}MB limit and was not loaded: {source}",
        )

    async def _attach_bytes(
        self,
        content_bytes: bytes,
        source: str,
        tool_result: ToolMessage,
        tool_call_id: str,
    ) -> ToolMessage:
        """Type, verify and encode fetched bytes onto the tool result.

        Typed from the bytes rather than the name: a signed or redirected URL can
        end without an extension, and a sandbox extension is only as accurate as
        whatever wrote the file — a truncated chart named .png is checkpointed
        before any provider sees it.
        """
        try:
            # Off-loop: pypdf walks the xref of up to MAX_CONTENT_BYTES of
            # model-chosen content, which is milliseconds on a real document
            # but ~100ms on one padded with junk after %%EOF.
            mime_type, pages = await asyncio.to_thread(_detect_mime_type, content_bytes)
        except _UnusableContent as exc:
            logger.warning(f"[MULTIMODAL] Refusing content ({exc}): {source}")
            return _error(tool_call_id, f"Content is {exc}: {source}")

        limit = self._limit_for(mime_type)
        if len(content_bytes) > limit:
            return self._too_large(source, tool_call_id, limit)

        b64_string = base64.b64encode(content_bytes).decode("utf-8")
        logger.info(
            f"[MULTIMODAL] Attaching content to the tool result: {source} "
            f"({len(content_bytes)} bytes, {mime_type})"
        )
        return attach_to_tool_result(
            tool_result, build_content_blocks(b64_string, source, mime_type, pages)
        )

    async def _handle_url_content(
        self,
        url: str,
        tool_result: ToolMessage,
        tool_call_id: str,
    ) -> ToolMessage:
        """Download a URL and attach its bytes, or answer with why it could not."""
        try:
            # The URL is model-chosen and this fetch runs in the backend process
            # — not the sandbox — so it inherits the backend's reach into the
            # private network. GuardedAsyncTransport re-checks the resolved
            # address on every hop, which is what keeps a public page from
            # redirecting the fetch at 169.254.169.254.
            async with httpx.AsyncClient(
                transport=GuardedAsyncTransport(), timeout=30.0
            ) as client:
                async with client.stream("GET", url, follow_redirects=True) as response:
                    if response.status_code != 200:
                        logger.warning(
                            f"[MULTIMODAL] Failed to download content: {url} "
                            f"(status {response.status_code})"
                        )
                        return _error(
                            tool_call_id,
                            f"Could not download content (HTTP {response.status_code}): {url}",
                        )

                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > self._fetch_limit():
                            return self._too_large(url, tool_call_id, self._fetch_limit())
                    content_bytes = bytes(buffer)

            if not content_bytes:
                logger.warning(f"[MULTIMODAL] Empty content from URL: {url}")
                return _error(tool_call_id, f"Empty content from URL: {url}")

            return await self._attach_bytes(
                content_bytes, url, tool_result, tool_call_id
            )

        except httpx.TimeoutException:
            logger.warning(f"[MULTIMODAL] Timeout downloading content: {url}")
            return _error(tool_call_id, f"Timeout downloading content: {url}")
        except BlockedAddressError as e:
            # The message names the resolved address ("Blocked private/reserved
            # address 10.1.2.3 for host x"). The URL is model-chosen, so echoing
            # that back turns Read into a DNS-to-IP probe steerable by anything
            # the agent reads. Keep the detail in the log, not in the transcript.
            logger.warning(f"[MULTIMODAL] Blocked address for {url}: {e}")
            return _error(tool_call_id, f"This address is not allowed: {url}")
        except httpx.RequestError as e:
            logger.warning(f"[MULTIMODAL] Network error downloading content {url}: {e}")
            return _error(tool_call_id, f"Network error downloading content: {e}")
        except Exception as e:
            logger.warning(f"[MULTIMODAL] Unexpected error handling URL {url}: {e}")
            return _error(tool_call_id, f"Failed to load content: {e}")

    async def _handle_sandbox_content(
        self,
        file_path: str,
        tool_result: ToolMessage,
        tool_call_id: str,
    ) -> ToolMessage:
        """Fetch a sandbox file's bytes and attach them, or answer with why not."""
        try:
            # The Read tool reaches the sandbox through SandboxBackend, which
            # normalizes first; this middleware holds the sandbox itself, whose
            # adownload_file_bytes does not. Without this, a virtual path the
            # tool resolved fine ("/work/task/charts/c.png") misses here and
            # replaces the tool's success with a not-found error.
            file_bytes = await self.sandbox.adownload_file_bytes(
                self.sandbox.normalize_path(file_path)
            )
            if not file_bytes:
                logger.warning(f"[MULTIMODAL] Failed to download file: {file_path}")
                return _error(tool_call_id, f"Could not read file: {file_path}")

            if len(file_bytes) > self._fetch_limit():
                return self._too_large(file_path, tool_call_id, self._fetch_limit())

            return await self._attach_bytes(
                file_bytes, file_path, tool_result, tool_call_id
            )

        except (OSError, ValueError) as e:
            logger.warning(f"[MULTIMODAL] Error loading sandbox file {file_path}: {e}")
            return _error(tool_call_id, f"Failed to load file: {e}")


__all__ = ["MultimodalMiddleware", "attach_to_tool_result", "build_content_blocks"]
