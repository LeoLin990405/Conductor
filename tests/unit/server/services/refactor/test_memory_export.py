from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.server.services.refactor.memory_export import (
    MemoryExportError,
    export_memory_page,
)


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.search_calls = []

    async def asearch(self, namespace, *, limit, offset):
        prefix = tuple(namespace)
        self.search_calls.append((prefix, limit, offset))
        matches = [row for row in self.rows if row.namespace[: len(prefix)] == prefix]
        return matches[offset : offset + limit]

    async def aget(self, namespace, key):
        target = (*tuple(namespace), key)
        return next(
            (row for row in self.rows if (*row.namespace, row.key) == target), None
        )


def _item(namespace, key, content, *, sensitivity="private"):
    if sensitivity:
        content = f"---\nsensitivity: {sensitivity}\n---\n{content}"
    return SimpleNamespace(
        namespace=tuple(namespace),
        key=key,
        value={"content": content},
        updated_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_export_uses_store_bodies_scopes_and_excludes_secret_index_pointers():
    user = ("user-1", "memory")
    workspace = ("user-1", "workspaces", "workspace-1", "memory")
    session = ("user-1", "threads", "thread-1", "memory")
    rows = [
        _item(user, "memory.md", "# Memory\n- [Public](public.md) — safe\n- [Secret](secret.md) — hidden\n"),
        _item(user, "public.md", "canonical public body"),
        _item(user, "secret.md", "canonical secret body", sensitivity="secret"),
        _item(workspace, "workspace.md", "workspace body"),
        _item(session, "session.md", "session body"),
    ]
    store = FakeStore(rows)

    page = await export_memory_page(store=store, user_id="user-1", limit=20)

    assert page["next_cursor"] is None
    by_key = {item["key"]: item for item in page["items"]}
    assert set(by_key) == {"memory.md", "public.md", "workspace.md", "session.md"}
    assert by_key["public.md"]["content"].endswith("canonical public body")
    assert "Public" in by_key["memory.md"]["content"]
    assert "Secret" not in by_key["memory.md"]["content"]
    assert by_key["memory.md"]["redacted"] is True
    assert by_key["memory.md"]["redacted_pointer_count"] == 1
    assert by_key["workspace.md"]["scope"] == {
        "tier": "workspace",
        "workspace_id": "workspace-1",
    }
    assert by_key["session.md"]["scope"] == {
        "tier": "session",
        "thread_id": "thread-1",
    }
    assert by_key["public.md"]["updated_at"] == "2026-11-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_export_redacts_bracket_title_secret_and_missing_pointers():
    namespace = ("owner", "memory")
    rows = [
        _item(
            namespace,
            "memory.md",
            "# Memory\n- [Forgotten [secret]](secret.md) — hidden\n"
            "- [Missing [entry]](missing.md) — unknown\n"
            "- [Safe [nested] title](safe.md) — visible\n",
        ),
        _item(namespace, "secret.md", "secret detail", sensitivity="secret"),
        _item(namespace, "safe.md", "safe detail"),
    ]

    page = await export_memory_page(store=FakeStore(rows), user_id="owner")
    index = next(item for item in page["items"] if item["key"] == "memory.md")

    assert "Forgotten" not in index["content"]
    assert "Missing" not in index["content"]
    assert "Safe [nested] title" in index["content"]
    assert index["redacted_pointer_count"] == 2


@pytest.mark.asyncio
async def test_export_drops_malformed_pointer_shaped_lines_without_valid_matches():
    namespace = ("owner", "memory")
    rows = [
        _item(
            namespace,
            "memory.md",
            "# Memory\n- [Broken [title](secret.md — hidden\n"
            "- [Another malformed] no destination\n",
        ),
        _item(namespace, "secret.md", "secret detail", sensitivity="secret"),
    ]

    page = await export_memory_page(store=FakeStore(rows), user_id="owner")
    index = next(item for item in page["items"] if item["key"] == "memory.md")

    assert index["content"] == "# Memory\n"
    assert index["redacted"] is True
    assert index["redacted_pointer_count"] == 2


@pytest.mark.asyncio
async def test_export_drops_ambiguous_nested_link_title_instead_of_verifying_inner_path():
    namespace = ("owner", "memory")
    rows = [
        _item(
            namespace,
            "memory.md",
            "# Memory\n- [Title [nested](safe.md) more](secret.md) — hidden\n",
        ),
        _item(namespace, "safe.md", "safe detail"),
        _item(namespace, "secret.md", "secret detail", sensitivity="secret"),
    ]

    page = await export_memory_page(store=FakeStore(rows), user_id="owner")
    index = next(item for item in page["items"] if item["key"] == "memory.md")

    assert "safe.md" not in index["content"]
    assert "secret.md" not in index["content"]
    assert index["redacted_pointer_count"] == 1


@pytest.mark.asyncio
async def test_export_include_secret_preserves_original_bracket_title_index():
    namespace = ("owner", "memory")
    content = "# Memory\n- [Forgotten [secret]](secret.md) — hidden\n"
    store = FakeStore(
        [
            _item(namespace, "memory.md", content),
            _item(namespace, "secret.md", "secret detail", sensitivity="secret"),
        ]
    )

    page = await export_memory_page(
        store=store, user_id="owner", include_secret=True
    )
    index = next(item for item in page["items"] if item["key"] == "memory.md")

    assert index["content"].endswith(content)
    assert "redacted" not in index
    assert "secret.md" in {item["key"] for item in page["items"]}


@pytest.mark.asyncio
async def test_export_cursor_pages_are_bounded_and_secret_requires_explicit_opt_in():
    namespace = ("owner", "memory")
    store = FakeStore(
        [
            _item(namespace, "a.md", "one"),
            _item(namespace, "b.md", "two", sensitivity="secret"),
            _item(namespace, "c.md", "three"),
        ]
    )

    first = await export_memory_page(store=store, user_id="owner", limit=1)
    second = await export_memory_page(
        store=store, user_id="owner", limit=1, cursor=first["next_cursor"]
    )
    third = await export_memory_page(
        store=store, user_id="owner", limit=1, cursor=second["next_cursor"]
    )

    assert first["items"][0]["key"] == "a.md"
    assert second["items"][0]["key"] == "c.md"
    assert second["next_cursor"] == "0:3"
    assert third["items"] == []
    assert third["next_cursor"] is None
    assert all(call[1] <= 200 for call in store.search_calls)

    secret_page = await export_memory_page(
        store=store, user_id="owner", limit=10, include_secret=True
    )
    assert "b.md" in {item["key"] for item in secret_page["items"]}


@pytest.mark.asyncio
async def test_export_marks_unverified_pointers_beyond_detail_check_cap_as_redacted():
    namespace = ("owner", "memory")
    pointers = [f"- [fact {index}](fact-{index}.md)" for index in range(33)]
    rows = [_item(namespace, "memory.md", "# Memory\n" + "\n".join(pointers))]
    rows.extend(
        _item(namespace, f"fact-{index}.md", f"body {index}") for index in range(32)
    )

    page = await export_memory_page(
        store=FakeStore(rows), user_id="owner", limit=50
    )
    index = next(item for item in page["items"] if item["key"] == "memory.md")

    assert index["redacted"] is True
    assert index["redacted_pointer_count"] == 1
    assert "fact-32.md" not in index["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["x", "3:0", "0:-1", "0:1000001", "0:1:2"])
async def test_export_rejects_invalid_cursors(cursor):
    with pytest.raises(MemoryExportError, match="cursor"):
        await export_memory_page(
            store=FakeStore([]), user_id="owner", cursor=cursor
        )


def test_export_policy_does_not_close_frontmatter_inside_a_line():
    from src.server.services.refactor import memory_export

    assert memory_export._policy(
        "---\ndescription: Draft---\nsensitivity: secret\n---\nbody"
    )["sensitivity"] == "secret"
