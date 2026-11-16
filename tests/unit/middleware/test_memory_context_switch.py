"""Phase-2 render switch: selected memory block replaces the per-tier indices.

Two invariants: the switch helper never mutates the shared build inputs (the
sources value is reused by every agent built from one session/turn), and the
selected block renders through the harness-block path, exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ptc_agent.agent.middleware.runtime_context.baseline import (
    BaselineSources,
    MemoryTierSource,
    with_memory_context_block,
)
from ptc_agent.agent.middleware.runtime_context.epoch import (
    BaselineEpoch,
    FileEntry,
    Observations,
    SourceRead,
    advance_epoch,
)
from ptc_agent.agent.middleware.runtime_context.harness_blocks import HARNESS_BLOCKS
from ptc_agent.agent.middleware.runtime_context.templates import render_template


def _sources() -> BaselineSources:
    return BaselineSources(
        store=object(),
        memory={
            "user": MemoryTierSource(
                namespace_factory=lambda: ("user", "memory"),
                display_path=".agents/user/memory.md",
            ),
            "workspace": MemoryTierSource(
                namespace_factory=lambda: ("user", "ws", "memory"),
                display_path=".agents/workspace/memory.md",
            ),
        },
    )


class TestSwitchHelper:
    def test_registered_as_a_harness_block(self):
        entry = HARNESS_BLOCKS["memory_context"]
        assert entry.template == "envelope/baseline_memory_context.md.j2"
        assert entry.label == "<memory-context>"

    def test_suppresses_legacy_tiers_and_merges_the_block(self):
        sources, blocks = with_memory_context_block(
            _sources(), {"skills": "skills manifest"}, "- [user] prefs/lang: zh"
        )
        assert sources.memory == {}
        assert blocks == {
            "skills": "skills manifest",
            "memory_context": "- [user] prefs/lang: zh",
        }

    def test_inputs_are_not_mutated(self):
        original = _sources()
        blocks = {"skills": "s"}
        with_memory_context_block(original, blocks, "block")
        assert set(original.memory) == {"user", "workspace"}
        assert blocks == {"skills": "s"}

    def test_empty_selection_still_suppresses_legacy(self):
        sources, blocks = with_memory_context_block(_sources(), None, "")
        assert sources.memory == {}
        assert blocks == {"memory_context": ""}


class TestTemplate:
    def test_renders_selected_index_block_once(self):
        rendered = render_template(
            HARNESS_BLOCKS["memory_context"].template,
            content="- [user] prefs/lang: prefers Chinese\n- [task] task/remaining: charts",
            guidance=None,
            subagent=False,
        )
        assert "<memory-context" in rendered
        assert rendered.count("prefs/lang") == 1
        assert rendered.count("task/remaining") == 1

    def test_empty_content_renders_nothing(self):
        rendered = render_template(
            HARNESS_BLOCKS["memory_context"].template,
            content="",
            guidance=None,
            subagent=False,
        )
        assert rendered == ""


class TestForcedRebuildOnMemoryDisable:
    """Opting out of automatic memory must un-render the frozen block.

    A change row only says the source moved; it cannot remove a system block
    that is already rendered, so a thread that froze a selected memory block
    before the user opted out needs an epoch rebuild.
    """

    @staticmethod
    def _epoch_with_selected_memory() -> BaselineEpoch:
        return BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="- [user] prefs/lang: prefers Chinese",
                    sha256="frozen",
                    exists=True,
                )
            },
        )

    @staticmethod
    def _empty_selection() -> tuple[SourceRead, ...]:
        return (
            SourceRead(
                kind="memory_context",
                update_kind="memory_context_changed",
                path="<memory-context>",
                text="",
            ),
        )

    @staticmethod
    def _selection(*keys: str) -> tuple[SourceRead, ...]:
        text = "\n".join(
            f"- [user][conductor_pg] {key}: selected text" for key in keys
        )
        return (
            SourceRead(
                kind="memory_context",
                update_kind="memory_context_changed",
                path="<memory-context>",
                text=text,
            ),
        )

    def test_disabled_memory_rebuilds_without_the_frozen_block(self):
        epoch = self._epoch_with_selected_memory()
        observations = Observations(
            now=datetime.now(tz=UTC),
            reads=self._empty_selection(),
            force_memory_rebuild=True,
        )

        rebuilt, rows = advance_epoch(epoch, observations)

        assert rebuilt is not None
        assert rebuilt.reason == "memory_disabled"
        assert rebuilt.blocks["memory_context"].text == ""
        assert rows == []

    def test_an_empty_selection_revokes_the_frozen_block_without_the_flag(self):
        epoch = self._epoch_with_selected_memory()
        observations = Observations(
            now=datetime.now(tz=UTC),
            reads=self._empty_selection(),
        )

        rebuilt, rows = advance_epoch(epoch, observations)

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert rebuilt.blocks["memory_context"].text == ""
        assert rows == []

    def test_a_dropped_entry_revokes_the_frozen_block(self):
        # Two entries were rendered; the new selection keeps only one. The
        # dropped entry may have become secret/superseded/expired, so the old
        # block must not stay in front of the model.
        epoch = self._epoch_with_selected_memory()
        epoch = BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="\n".join(
                        [
                            "- [user][conductor_pg] prefs/lang: prefers Chinese",
                            "- [user][conductor_pg] secret/strategy: do not reveal",
                        ]
                    ),
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        observations = Observations(
            now=datetime.now(tz=UTC),
            reads=self._selection("prefs/lang"),
        )

        rebuilt, rows = advance_epoch(epoch, observations)

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert "secret/strategy" not in rebuilt.blocks["memory_context"].text
        assert "prefs/lang" in rebuilt.blocks["memory_context"].text
        assert rows == []

    def test_revocation_compares_tier_and_source_not_just_the_key(self):
        # The same entry key can name one user-tier and one workspace-tier
        # entry. Revoking the user entry must not look "present" because the
        # workspace line kept the same bare key.
        epoch = BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="\n".join(
                        [
                            "- [user][conductor_pg] shared.md: user text",
                            "- [workspace][conductor_pg] shared.md: workspace text",
                        ]
                    ),
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        fresh = SourceRead(
            kind="memory_context",
            update_kind="memory_context_changed",
            path="<memory-context>",
            text="- [workspace][conductor_pg] shared.md: workspace text",
        )

        rebuilt, _rows = advance_epoch(
            epoch, Observations(now=datetime.now(tz=UTC), reads=(fresh,))
        )

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert "user text" not in rebuilt.blocks["memory_context"].text

    def test_source_change_for_the_same_key_also_revokes(self):
        epoch = BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="- [user][memfw] shared.md: old source text",
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        fresh = SourceRead(
            kind="memory_context",
            update_kind="memory_context_changed",
            path="<memory-context>",
            text="- [user][conductor_pg] shared.md: conductor text",
        )

        rebuilt, _rows = advance_epoch(
            epoch, Observations(now=datetime.now(tz=UTC), reads=(fresh,))
        )

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"

    def test_a_selected_key_with_spaces_still_parses_as_an_identity(self):
        epoch = BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="- [user][conductor_pg] secret note.md: sensitive body",
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        fresh = SourceRead(
            kind="memory_context",
            update_kind="memory_context_changed",
            path="<memory-context>",
            text="",
        )

        rebuilt, _rows = advance_epoch(
            epoch, Observations(now=datetime.now(tz=UTC), reads=(fresh,))
        )

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert rebuilt.blocks["memory_context"].text == ""

    def test_added_entries_do_not_force_a_rebuild(self):
        epoch = self._epoch_with_selected_memory()
        epoch = BaselineEpoch(
            stored=True,
            blocks={
                "memory_context": FileEntry(
                    path="<memory-context>",
                    text="- [user][conductor_pg] prefs/lang: prefers Chinese",
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        observations = Observations(
            now=datetime.now(tz=UTC),
            reads=self._selection("prefs/lang", "new/fact"),
        )

        advanced, rows = advance_epoch(epoch, observations)

        assert advanced is not None
        # Nothing was revoked: the frozen block stays and the change rides a
        # durable row, which is the design's normal path.
        assert advanced.blocks["memory_context"].text == (
            "- [user][conductor_pg] prefs/lang: prefers Chinese"
        )
        assert any("memory_context" in row.kind for row in rows)


class TestLegacyTierRevocation:
    """With memory_index off, the per-tier index is rendered instead of the
    selected block; revoking a detail removes its pointer from that index."""

    @staticmethod
    def _epoch_with_user_index() -> BaselineEpoch:
        return BaselineEpoch(
            stored=True,
            memory={
                "user": FileEntry(
                    path=".agents/user/memory.md",
                    text="\n".join(
                        [
                            "- [Secret](secret/strategy.md) — do not reveal",
                            "- [Language](prefs/lang.md) — prefers Chinese",
                        ]
                    ),
                    sha256="frozen",
                    exists=True,
                )
            },
        )

    def test_a_pointer_dropped_from_the_legacy_index_forces_a_rebuild(self):
        fresh = SourceRead(
            kind="memory:user",
            update_kind="memory_changed:user",
            path=".agents/user/memory.md",
            text="- [Language](prefs/lang.md) — prefers Chinese",
        )
        rebuilt, rows = advance_epoch(
            self._epoch_with_user_index(),
            Observations(now=datetime.now(tz=UTC), reads=(fresh,)),
        )

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert "secret/strategy" not in rebuilt.memory["user"].text
        assert rows == []

    def test_a_bracket_bearing_pointer_title_is_still_tracked(self):
        # `[Client [private]](secret.md)` is a valid pointer line; revocation
        # must notice when it disappears even though the title has brackets.
        epoch = BaselineEpoch(
            stored=True,
            memory={
                "user": FileEntry(
                    path=".agents/user/memory.md",
                    text="- [Client [private]](secret.md) — do not reveal",
                    sha256="frozen",
                    exists=True,
                )
            },
        )
        fresh = SourceRead(
            kind="memory:user",
            update_kind="memory_changed:user",
            path=".agents/user/memory.md",
            text="",
        )

        rebuilt, _rows = advance_epoch(
            epoch, Observations(now=datetime.now(tz=UTC), reads=(fresh,))
        )

        assert rebuilt is not None
        assert rebuilt.reason == "memory_revoked"
        assert rebuilt.memory["user"].text == ""

    def test_an_added_pointer_keeps_the_legacy_index_frozen(self):
        fresh = SourceRead(
            kind="memory:user",
            update_kind="memory_changed:user",
            path=".agents/user/memory.md",
            text=(
                "- [Secret](secret/strategy.md) — do not reveal\n"
                "- [Language](prefs/lang.md) — prefers Chinese\n"
                "- [Fact](new/fact.md) — added later"
            ),
        )
        advanced, rows = advance_epoch(
            self._epoch_with_user_index(),
            Observations(now=datetime.now(tz=UTC), reads=(fresh,)),
        )

        assert advanced is not None
        assert advanced.memory["user"].text == (
            "- [Secret](secret/strategy.md) — do not reveal\n"
            "- [Language](prefs/lang.md) — prefers Chinese"
        )
        assert any("memory" in row.kind for row in rows)


def test_context_stack_passes_the_forced_rebuild_flag_to_the_baseline():
    from ptc_agent.agent.context_stack import build_context_middleware

    context = build_context_middleware(
        now=datetime.now(tz=UTC),
        guidance=None,
        model_name=None,
        force_memory_rebuild=True,
    )
    assert context.baseline._force_memory_rebuild is True
