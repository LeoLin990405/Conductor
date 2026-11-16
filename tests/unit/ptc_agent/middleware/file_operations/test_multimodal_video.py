"""Video input: container typing, wire-block construction and read-side stripping.

The pipeline is three seams that fail in different places:

- injection (``_detect_mime_type`` / ``build_content_blocks``), which checkpoints
  whatever it builds, so a wrong decision replays a 400 on every later turn;
- the tool-result carrier, which must stay a block list rather than being
  stringified into the provider as a wall of base64;
- the read-side strip, which is the only thing that can save a mid-thread switch
  to a model without the video modality.

Only the OpenAI-compatible ``video_url`` envelope is written today; StepFun's
Chat API documents exactly that shape (a base64 data URI is accepted), and a
live call against ``step-5-preview`` is recorded in docs/VIDEO-INPUT.md.
"""

import base64
import io
import types

import httpx
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from PIL import Image

from ptc_agent.agent.middleware.file_operations import multimodal
from ptc_agent.agent.middleware.file_operations.multimodal import (
    VIDEO_EXTENSIONS,
    MultimodalMiddleware,
    _detect_mime_type,
    _is_visual_request,
    build_content_blocks,
)
from ptc_agent.agent.middleware.file_operations.multimodal_strip import (
    PLACEHOLDER_GUIDANCE,
    MultimodalStripMiddleware,
    strip_unsupported_content_blocks,
)
from ptc_agent.agent.middleware.compaction.offloading import (
    _extract_base64_info,
    aoffload_base64_content,
)
from ptc_agent.agent.middleware.compaction.utils import (
    _extract_text_from_content,
    strip_base64_from_content,
)
from ptc_agent.agent.tools.file_ops import create_filesystem_tools
from src.llms.attachment_payload import has_attachment, is_attachment_payload
from src.llms.llm import get_input_modalities


# --- fixtures ---------------------------------------------------------------


def _box(box_type: bytes, payload: bytes) -> bytes:
    """One ISO base media box: big-endian size, four-byte type, payload."""
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _ftyp(major: bytes, compatible: tuple[bytes, ...] = ()) -> bytes:
    """An ``ftyp`` box: major brand, minor version, then compatible brands."""
    return _box(b"ftyp", major + b"\x00\x00\x02\x00" + b"".join(compatible))


def _moov(handler: bytes = b"vide") -> bytes:
    """A minimal ``moov`` holding one track whose handler names the media type."""
    hdlr = _box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 12)
    return _box(b"moov", _box(b"trak", _box(b"mdia", hdlr)))


def _mp4(
    major: bytes = b"isom",
    compatible: tuple[bytes, ...] = (b"isom", b"iso2", b"avc1", b"mp41"),
    *,
    with_moov: bool = True,
    handler: bytes = b"vide",
) -> bytes:
    """A structurally plausible MP4. ``mdat`` leads so ``moov`` is at the end,
    which is how a non-faststart muxer writes it."""
    mdat = _box(b"mdat", b"\x00" * 64)
    tail = _moov(handler) if with_moov else b""
    return _ftyp(major, compatible) + mdat + tail


def _ebml(doctype: bytes) -> bytes:
    """An EBML header carrying ``doctype``; the rest is filler."""
    return b"\x1aE\xdf\xa3" + b"\x9f\x42\x82\x88" + doctype + b"\x00" * 16


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buf, format="PNG")
    return buf.getvalue()


class _Sandbox:
    """Stands in for PTCSandbox — which normalizes on its own, not on download."""

    def __init__(self, content: bytes = b"", *, work_dir: str = "/home/workspace"):
        self._content = content
        self._work_dir = work_dir
        self.downloaded: str | None = None

    def normalize_path(self, path: str) -> str:
        if path.startswith((self._work_dir, "/tmp")):
            return path
        return f"{self._work_dir}/{path.lstrip('/')}"

    async def adownload_file_bytes(self, path: str) -> bytes:
        self.downloaded = path
        return self._content


def _ack() -> ToolMessage:
    """The Read tool's own result, which is what the handlers are handed."""
    return ToolMessage(content="ok", tool_call_id="call-1")


def _sandbox_read(file_path: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        tool_call={"name": "Read", "args": {"file_path": file_path}, "id": "tc-1"}
    )


class _ModelCallRequest:
    """Minimal model-call request: a stamped client, a history and ``override``."""

    def __init__(self, manifest_model: str, messages: list):
        self.model = types.SimpleNamespace(metadata={"manifest_model": manifest_model})
        self.messages = messages
        self.system_message = None

    def override(self, **kwargs):
        clone = _ModelCallRequest.__new__(_ModelCallRequest)
        clone.model = self.model
        clone.messages = kwargs.get("messages", self.messages)
        clone.system_message = kwargs.get("system_message", self.system_message)
        return clone


def _video_history() -> list:
    return [
        HumanMessage(
            content=[
                {"type": "text", "text": "Describe this clip"},
                {
                    "type": "video_url",
                    "video_url": {"url": "data:video/mp4;base64,AAAA"},
                },
            ]
        ),
    ]


# --- container typing -------------------------------------------------------


class TestVideoMimeDetection:
    def test_an_mp4_with_a_video_brand_is_typed_from_the_ftyp_box(self):
        assert _detect_mime_type(_mp4())[0] == "video/mp4"

    def test_quicktime_is_typed_from_its_qt_brand(self):
        assert _detect_mime_type(_mp4(major=b"qt  ", compatible=(b"qt  ",)))[0] == (
            "video/quicktime"
        )

    def test_an_unlisted_brand_falls_back_to_the_moov_track(self):
        """Some muxers write only brands we do not enumerate. The movie box
        declares a video track, so the file is a video even so."""
        raw = _mp4(major=b"XXXX", compatible=(b"XXXX",))
        assert _detect_mime_type(raw)[0] == "video/mp4"

    def test_a_moov_with_no_video_track_is_not_a_video(self):
        raw = _mp4(major=b"XXXX", compatible=(b"XXXX",), handler=b"soun")
        with pytest.raises(multimodal._UnusableContent):
            _detect_mime_type(raw)

    def test_an_avif_still_image_is_not_relabelled_as_video(self):
        """``ftyp`` also opens AVIF/HEIF images. A brand-only check would ship one
        as ``video/mp4``, and that block is checkpointed and replayed."""
        raw = _ftyp(b"avif", (b"avif", b"mif1", b"miaf"))
        with pytest.raises(multimodal._UnusableContent) as excinfo:
            _detect_mime_type(raw)
        assert "not a readable image, PDF or video" in str(excinfo.value)

    def test_an_ftyp_header_alone_is_not_enough(self):
        with pytest.raises(multimodal._UnusableContent):
            _detect_mime_type(b"\x00\x00\x00\x10ftypisom")

    def test_webm_is_typed_from_its_ebml_doctype(self):
        assert _detect_mime_type(_ebml(b"webm"))[0] == "video/webm"

    def test_matroska_is_typed_from_its_ebml_doctype(self):
        assert _detect_mime_type(_ebml(b"matroska"))[0] == "video/x-matroska"

    def test_an_ebml_stream_of_unknown_doctype_is_refused(self):
        with pytest.raises(multimodal._UnusableContent):
            _detect_mime_type(_ebml(b"   ") + b"\x00" * 8)

    def test_video_detection_does_not_shadow_images(self):
        assert _detect_mime_type(_png_bytes())[0] == "image/png"

    def test_every_supported_extension_is_routed(self):
        assert VIDEO_EXTENSIONS == frozenset({".mp4", ".mov", ".webm", ".mkv"})
        for suffix in (".mp4", ".mov", ".webm", ".mkv", ".MP4"):
            assert _is_visual_request(f"clips/demo{suffix}"), suffix

    def test_a_memo_tier_video_is_not_intercepted(self):
        """The memo tier serves extracted text from the store and has no
        sandbox-FS copy, so intercepting would replace content with not-found."""
        assert not _is_visual_request(".agents/user/memo/clip.mp4")

    def test_a_url_is_still_intercepted_without_an_extension(self):
        assert _is_visual_request("https://cdn.example.com/d/abc123")


class TestReadToolRoutesVideos:
    """The tool acknowledges before the middleware fetches the bytes, so a video
    extension the Read tool treats as text never reaches the video path at all."""

    @pytest.mark.asyncio
    async def test_mp4_read_returns_the_video_sentinel(self):
        backend = types.SimpleNamespace(
            filesystem_config=types.SimpleNamespace(enable_path_validation=False),
            normalize_path=lambda path: f"/home/workspace/{path.lstrip('/')}",
        )
        read = {tool.name: tool for tool in create_filesystem_tools(backend)}["Read"]

        result = await read.ainvoke({"file_path": "/home/workspace/clip.mp4"})

        assert result == "Loading video: /home/workspace/clip.mp4"

    def test_the_middleware_reads_the_same_extension_set(self):
        from ptc_agent.agent.tools import file_ops

        assert file_ops.VIDEO_EXTENSIONS == VIDEO_EXTENSIONS
        assert VIDEO_EXTENSIONS <= file_ops.VISUAL_EXTENSIONS


# --- wire block -------------------------------------------------------------


class TestVideoBlockConstruction:
    def test_a_video_becomes_a_video_url_data_uri(self):
        blocks = build_content_blocks("QUJD", "clips/demo.mp4", "video/mp4")

        assert [b["type"] for b in blocks] == ["text", "video_url"]
        assert blocks[0]["text"] == "[Viewing video: demo.mp4]"
        assert blocks[1] == {
            "type": "video_url",
            "video_url": {"url": "data:video/mp4;base64,QUJD"},
        }

    def test_the_mime_type_rides_in_the_data_uri(self):
        blocks = build_content_blocks("QUJD", "clips/demo.mkv", "video/x-matroska")
        assert blocks[1]["video_url"]["url"].startswith("data:video/x-matroska;base64,")

    def test_a_video_block_is_an_attachment_payload(self):
        """``ToolResultNormalizationMiddleware`` exempts attachment payloads from
        stringification. Without this the model would receive the base64 as
        prose instead of a video it can look at."""
        blocks = build_content_blocks("QUJD", "clips/demo.mp4", "video/mp4")

        assert is_attachment_payload(blocks)
        assert has_attachment(blocks)

    def test_a_video_url_envelope_without_a_url_is_not_an_attachment(self):
        bad = [{"type": "text", "text": "x"}, {"type": "video_url", "video_url": {}}]
        assert not is_attachment_payload(bad)


class TestTheWriteSideInjectsVideos:
    @pytest.mark.asyncio
    async def test_a_sandbox_mp4_is_attached_to_the_read_result(self):
        mw = MultimodalMiddleware(sandbox=_Sandbox(_mp4()))

        async def handler(_request):
            return ToolMessage(content="Loading video: clip.mp4", tool_call_id="tc-1")

        result = await mw.awrap_tool_call(_sandbox_read("clip.mp4"), handler)

        assert [b["type"] for b in result.content] == ["text", "video_url"]
        assert result.content[0]["text"] == "Loading video: clip.mp4\n[Viewing video: clip.mp4]"
        url = result.content[1]["video_url"]["url"]
        assert url.startswith("data:video/mp4;base64,")
        # The bytes that were attached are the bytes that were fetched.
        assert base64.b64decode(url.split(";base64,", 1)[1]) == _mp4()

    @pytest.mark.asyncio
    async def test_the_bytes_decide_the_container_not_the_extension(self):
        """A file named .mov whose bytes are WebM is labelled WebM; the read side
        and the provider both trust the MIME, not the name."""
        mw = MultimodalMiddleware(sandbox=_Sandbox(_ebml(b"webm")))

        result = await mw._handle_sandbox_content("clip.mov", _ack(), "call-1")

        assert "data:video/webm;base64," in result.content[1]["video_url"]["url"]

    @pytest.mark.asyncio
    async def test_an_extensionless_url_serving_a_video_is_typed_from_bytes(
        self, monkeypatch
    ):
        served = _mp4()
        monkeypatch.setattr(
            multimodal,
            "GuardedAsyncTransport",
            lambda: httpx.MockTransport(
                lambda request: httpx.Response(200, content=served)
            ),
        )
        mw = MultimodalMiddleware(sandbox=_Sandbox())

        result = await mw._handle_url_content(
            "https://files.example.com/d/abc123", _ack(), "call-1"
        )

        assert result.content[1]["type"] == "video_url"
        assert result.content[1]["video_url"]["url"].startswith("data:video/mp4;base64,")

    @pytest.mark.asyncio
    async def test_an_image_read_is_unchanged(self):
        mw = MultimodalMiddleware(sandbox=_Sandbox(_png_bytes()))

        result = await mw._handle_sandbox_content("chart.png", _ack(), "call-1")

        assert [b["type"] for b in result.content] == ["text", "image_url"]


# --- caps and refusals ------------------------------------------------------


class TestVideoSizeCap:
    def test_the_video_cap_is_its_own_constant_above_the_image_cap(self):
        """No provider publishes a per-video byte ceiling that binds the way
        Anthropic's 5 MiB image ceiling does; the inline-request ceiling does."""
        assert MultimodalMiddleware.MAX_VIDEO_BYTES == 10 * 1024 * 1024
        assert MultimodalMiddleware.MAX_VIDEO_BYTES > MultimodalMiddleware.MAX_CONTENT_BYTES

    @pytest.mark.asyncio
    async def test_an_oversized_video_is_refused_before_encoding(self, monkeypatch):
        oversized = _mp4() + b"\x00" * (2 * 1024 * 1024)
        mw = MultimodalMiddleware(sandbox=_Sandbox(oversized))
        monkeypatch.setattr(mw, "MAX_VIDEO_BYTES", 2 * 1024 * 1024)

        result = await mw._handle_sandbox_content("big.mp4", _ack(), "call-1")

        assert isinstance(result.content, str)
        assert "larger than the 2MB limit" in result.content
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_the_video_cap_does_not_loosen_the_image_cap(self, monkeypatch):
        """An image is still bounded by 5 MiB even though videos may be larger;
        letting the larger cap win would brick the thread with a legal-looking
        image that the provider rejects."""
        big_png = _png_bytes() + b"\x00" * (1024 * 1024)
        mw = MultimodalMiddleware(sandbox=_Sandbox(big_png))
        monkeypatch.setattr(mw, "MAX_CONTENT_BYTES", 1024 * 1024)

        result = await mw._handle_sandbox_content("chart.png", _ack(), "call-1")

        assert isinstance(result.content, str)
        assert "larger than the 1MB limit" in result.content

    @pytest.mark.asyncio
    async def test_a_video_between_the_two_caps_is_accepted(self, monkeypatch):
        padded = _mp4() + b"\x00" * (2 * 1024 * 1024)
        mw = MultimodalMiddleware(sandbox=_Sandbox(padded))
        monkeypatch.setattr(mw, "MAX_CONTENT_BYTES", 1024)
        monkeypatch.setattr(mw, "MAX_VIDEO_BYTES", 4 * 1024 * 1024)

        result = await mw._handle_sandbox_content("clip.mp4", _ack(), "call-1")

        assert result.content[1]["type"] == "video_url"


class TestInvalidVideoIsRefusedReadably:
    @pytest.mark.asyncio
    async def test_a_bogus_mp4_is_refused_with_a_readable_error(self):
        avif = _ftyp(b"avif", (b"avif", b"mif1"))
        mw = MultimodalMiddleware(sandbox=_Sandbox(avif))

        result = await mw._handle_sandbox_content("fake.mp4", _ack(), "call-1")

        assert isinstance(result.content, str)
        assert result.content.startswith("ERROR:")
        assert "not a readable image, PDF or video" in result.content
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_a_truncated_video_never_reaches_graph_state(self):
        whole = _mp4()
        mw = MultimodalMiddleware(sandbox=_Sandbox(whole[: len(whole) // 2]))

        result = await mw._handle_sandbox_content("cut.mp4", _ack(), "call-1")

        assert isinstance(result.content, str)


# --- read-side strip --------------------------------------------------------


class TestStripVideoBlocks:
    def test_a_text_only_target_gets_a_placeholder(self):
        result = strip_unsupported_content_blocks(
            _video_history(), has_image=False, has_pdf=False
        )

        assert result[0].content[1]["type"] == "text"
        assert "Video attached in a prior turn" in result[0].content[1]["text"]

    def test_has_video_defaults_to_false_and_fails_closed(self):
        """A caller that has not been taught about video is judged text-only:
        over-stripping costs a placeholder, under-stripping costs the turn."""
        assert strip_unsupported_content_blocks(
            _video_history(), has_image=True, has_pdf=True
        )[0].content[1]["type"] == "text"

    def test_a_video_target_keeps_the_block_untouched(self):
        msgs = _video_history()
        assert (
            strip_unsupported_content_blocks(
                msgs, has_image=False, has_pdf=False, has_video=True
            )
            is msgs
        )

    def test_the_guidance_rides_on_the_first_placeholder_only(self):
        msgs = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "first"},
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA"}},
                ]
            ),
            HumanMessage(
                content=[
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,BB"}},
                ]
            ),
        ]
        result = strip_unsupported_content_blocks(
            msgs, has_image=False, has_pdf=False, guidance="Extract frames."
        )
        placeholders = [b["text"] for m in result for b in m.content if "Video" in b.get("text", "")]
        assert len(placeholders) == 2
        assert "Extract frames." in placeholders[0]
        assert "Extract frames." not in placeholders[1]

    def test_an_image_only_target_strips_video_but_keeps_images(self):
        msgs = [
            HumanMessage(
                content=[
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,BB"}},
                ]
            )
        ]
        result = strip_unsupported_content_blocks(
            msgs, has_image=True, has_pdf=False, has_video=False
        )
        assert result[0].content[0]["type"] == "image_url"
        assert result[0].content[1]["type"] == "text"

    def test_the_checkpoint_copy_is_not_mutated(self):
        msgs = _video_history()
        strip_unsupported_content_blocks(msgs, has_image=False, has_pdf=False)
        assert msgs[0].content[1]["type"] == "video_url"


class TestStripMiddlewareReadsTheManifest:
    @pytest.mark.asyncio
    async def test_a_text_only_target_has_its_video_stripped(self):
        seen = {}

        async def handler(request):
            seen["request"] = request
            return "ok"

        await MultimodalStripMiddleware().awrap_model_call(
            _ModelCallRequest("glm-5.2", _video_history()), handler
        )

        forwarded = seen["request"]
        assert forwarded.messages[0].content[1]["type"] == "text"
        assert PLACEHOLDER_GUIDANCE in forwarded.messages[0].content[1]["text"]

    @pytest.mark.asyncio
    async def test_a_stepfun_target_keeps_the_video(self):
        """The manifest entry is what enables the drop; without ``video`` in
        models.json this target would get the placeholder instead."""
        seen = {}

        async def handler(request):
            seen["request"] = request
            return "ok"

        request = _ModelCallRequest("step-5-preview", _video_history())
        await MultimodalStripMiddleware().awrap_model_call(request, handler)

        assert seen["request"] is request
        assert seen["request"].messages[0].content[1]["type"] == "video_url"

    @pytest.mark.asyncio
    async def test_step_37_flash_keeps_the_video_too(self):
        seen = {}

        async def handler(request):
            seen["request"] = request
            return "ok"

        request = _ModelCallRequest("step-3.7-flash", _video_history())
        await MultimodalStripMiddleware().awrap_model_call(request, handler)

        assert seen["request"].messages[0].content[1]["type"] == "video_url"

    def test_the_stepfun_manifest_entries_declare_video(self):
        assert "video" in get_input_modalities("step-5-preview")
        assert "video" in get_input_modalities("step-3.7-flash")
        # The text-only sibling is untouched; this change is scoped to video.
        assert "video" not in get_input_modalities("step-3.5-flash")

    @pytest.mark.parametrize(
        "model_name",
        ["gemini-4-argon", "minimax-m3.1-flash"],
        ids=["gemini", "minimax"],
    )
    @pytest.mark.asyncio
    async def test_a_video_capable_model_on_another_protocol_still_gets_placeholder(
        self, model_name
    ):
        """Capability is not protocol. These entries declare ``video``, but their
        clients do not serialize the OpenAI-compatible block the read half writes
        — Gemini's converter raises ``ValueError`` on it — so sending it would be
        worse than the placeholder. They keep the modality declaration; a
        protocol-specific converter is the follow-up that would let them use it.
        """
        assert "video" in get_input_modalities(model_name)  # declared, yet stripped

        seen = {}

        async def handler(request):
            seen["request"] = request
            return "ok"

        await MultimodalStripMiddleware().awrap_model_call(
            _ModelCallRequest(model_name, _video_history()), handler
        )

        assert seen["request"].messages[0].content[1]["type"] == "text"

    @pytest.mark.asyncio
    async def test_a_custom_model_with_a_declared_video_modality_keeps_the_block(self):
        """A user-defined model is built on the OpenAI-compatible SDK, so the
        user's ``video`` modality declaration is the protocol contract too."""
        seen = {}

        async def handler(request):
            seen["request"] = request
            return "ok"

        middleware = MultimodalStripMiddleware(
            model_name="my-custom-vlm", custom_modalities=["text", "video"]
        )
        await middleware.awrap_model_call(
            _ModelCallRequest("my-custom-vlm", _video_history()), handler
        )

        assert seen["request"].messages[0].content[1]["type"] == "video_url"


# --- compaction must not turn the base64 into prose -------------------------


class _UploadBackend:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []

    async def aupload_files(self, files):
        self.uploads.extend(files)
        return types.SimpleNamespace(error=None)


def _video_tool_message() -> ToolMessage:
    data = base64.b64encode(_mp4()).decode()
    return ToolMessage(
        content=[
            {"type": "text", "text": "ok\n[Viewing video: clip.mp4]"},
            {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{data}"}},
        ],
        tool_call_id="call-1",
    )


class TestCompactionTreatsVideoAsAnAttachment:
    """Compaction renders history with ``get_buffer_string``, which turns a
    content block into ``str(block)`` — the whole base64 if it is still inline.
    Both paths that stand between a video and the summarizer must recognize the
    ``video_url`` envelope, or a long thread pays for its clips twice and the
    summarizer prompt becomes megabytes of prose."""

    def test_the_offloader_extracts_a_video_data_uri(self):
        info = _extract_base64_info(
            {
                "type": "video_url",
                "video_url": {"url": "data:video/mp4;base64,QUJD"},
            }
        )
        assert info == ("QUJD", "video/mp4", "video")

    def test_a_non_inline_video_reference_is_not_offloaded(self):
        assert (
            _extract_base64_info(
                {
                    "type": "video_url",
                    "video_url": {"url": "https://cdn.example.com/clip.mp4"},
                }
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_a_video_block_is_offloaded_to_a_file_the_agent_can_read_back(self):
        backend = _UploadBackend()

        out = await aoffload_base64_content(
            backend, [_video_tool_message()], thread_id="abcd1234"
        )

        text = out[0].content[1]["text"]
        assert text.startswith("[Video saved to ")
        assert text.endswith("— use Read to view]")
        path, raw = backend.uploads[0]
        assert path.endswith(".mp4")
        assert raw == _mp4()

    @pytest.mark.asyncio
    async def test_the_no_backend_fallback_redacts_the_video_base64(self):
        out = await aoffload_base64_content(None, [_video_tool_message()])

        assert [b.get("type") for b in out[0].content] == ["text", "text"]
        assert out[0].content[1]["text"] == "[Video]"

    def test_the_base64_stripper_redacts_video_blocks(self):
        content = [
            {"type": "text", "text": "look"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,QUJD"}},
        ]
        stripped = strip_base64_from_content(content)
        assert stripped[1] == {"type": "text", "text": "[Video]"}

    def test_the_token_counter_charges_the_video_placeholder(self):
        text = _extract_text_from_content(
            [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,QUJD"}}]
        )
        assert text == "[video]"
