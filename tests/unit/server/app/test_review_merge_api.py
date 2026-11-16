"""Wire contract for the server-side review queue and the merge gate.

The database is an in-memory store and the worktrees are real; the merge
success path is covered end-to-end in ``test_review_merge_e2e.py``.
"""

from __future__ import annotations

import uuid

import pytest

from tests.unit.server.review_merge_git import commit_file, head

pytestmark = pytest.mark.asyncio

REVIEWS = "/api/v1/reviews"
MERGES = "/api/v1/merges"
AUDIT = "/api/v1/review-audit"


async def _create_review(client, fleet, *, line="line", reviewer="repo", provider="codex"):
    response = await client.post(
        REVIEWS,
        json={
            "line": line,
            "reviewer": reviewer,
            "provider": provider,
            "root": str(fleet.root),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _verdict(client, fleet, review_id, verdict):
    response = await client.post(
        f"{REVIEWS}/{review_id}/verdict",
        json={"verdict": verdict, "root": str(fleet.root)},
    )
    return response


class TestReviewQueue:
    async def test_create_queues_the_review_with_the_cli_shape(self, client, fleet, review_store):
        body = await _create_review(client, fleet)

        assert body["line"] == "line"
        assert body["reviewer"] == "repo"
        assert body["provider"] == "codex"
        assert body["state"] == "queued"
        assert body["verdict"] is None
        assert body["head_sha"] == head(fleet.line)
        assert body["id"] == body["id"].lower()
        assert len(review_store.reviews) == 1

    async def test_refuses_a_line_reviewing_itself(self, client, fleet, review_store):
        response = await client.post(
            REVIEWS,
            json={"line": "line", "reviewer": "line", "root": str(fleet.root)},
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "self_review_refused"
        assert "cannot review itself" in detail["message"]
        assert detail["remediation"]
        assert review_store.reviews == {}

    async def test_refuses_self_review_through_a_symlinked_line(self, client, fleet):
        (fleet.root / "line-alias").symlink_to(fleet.root / "line")

        response = await client.post(
            REVIEWS,
            json={"line": "line", "reviewer": "line-alias", "root": str(fleet.root)},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "self_review_refused"

    async def test_unknown_line_is_a_structured_404(self, client, fleet):
        response = await client.post(
            REVIEWS,
            json={"line": "nope", "reviewer": "repo", "root": str(fleet.root)},
        )

        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["code"] == "line_not_found"
        assert detail["remediation"]

    async def test_root_mismatch_is_refused_before_any_worktree_read(
        self, client, fleet, tmp_path
    ):
        other = tmp_path / "other-root"
        other.mkdir()
        response = await client.post(
            REVIEWS,
            json={"line": "line", "reviewer": "repo", "root": str(other)},
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "worktree_root_mismatch"
        assert detail["detail"]["server_root"] == str(fleet.root.resolve())

    async def test_list_returns_queued_reviews_for_the_caller(self, client, fleet):
        first = await _create_review(client, fleet)
        second = await _create_review(client, fleet, reviewer="target")

        response = await client.get(REVIEWS)

        assert response.status_code == 200
        ids = [row["id"] for row in response.json()["reviews"]]
        assert ids == [second["id"], first["id"]]

    async def test_list_filters_by_line(self, client, fleet):
        await _create_review(client, fleet)
        response = await client.get(REVIEWS, params={"line": "other-line"})
        assert response.status_code == 200
        assert response.json()["reviews"] == []

    async def test_get_unknown_review_is_404(self, client, fleet):
        response = await client.get(f"{REVIEWS}/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "review_not_found"


class TestReviewVerdict:
    async def test_verdict_completes_and_binds_the_reviewed_head(self, client, fleet, review_store):
        created = await _create_review(client, fleet)
        line_head = head(fleet.line)

        response = await _verdict(client, fleet, created["id"], "merge")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "completed"
        assert body["verdict"] == "merge"
        assert body["head_sha"] == line_head
        assert body["completed_at"] is not None
        stored = review_store.reviews[created["id"]]
        assert stored["head_sha"] == line_head

    async def test_verdict_rejects_an_unknown_value(self, client, fleet):
        created = await _create_review(client, fleet)
        response = await client.post(
            f"{REVIEWS}/{created['id']}/verdict",
            json={"verdict": "lgtm", "root": str(fleet.root)},
        )
        assert response.status_code == 422

    async def test_a_finished_review_cannot_be_rejudged(self, client, fleet):
        created = await _create_review(client, fleet)
        first = await _verdict(client, fleet, created["id"], "merge")
        assert first.status_code == 200

        second = await _verdict(client, fleet, created["id"], "block")

        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "review_not_open"

    async def test_verdict_refuses_when_the_line_moved_after_queueing(
        self, client, fleet
    ):
        created = await _create_review(client, fleet)
        commit_file(fleet.line, "moved.txt", "moved\n", "advance after queueing")

        response = await _verdict(client, fleet, created["id"], "merge")

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "review_stale"
        assert detail["detail"]["reviewed_sha"] == created["head_sha"]
        assert detail["detail"]["head_sha"] == head(fleet.line)

    async def test_verdict_refuses_when_the_reviewed_line_is_gone(self, client, fleet):
        created = await _create_review(client, fleet)
        import shutil

        shutil.rmtree(fleet.line)

        response = await _verdict(client, fleet, created["id"], "merge")

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "line_not_found"


class TestMergeGate:
    async def test_remote_peer_never_reaches_merge(self, remote_client, fleet, review_store):
        response = await remote_client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
        )

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["code"] == "local_only"
        assert detail["remediation"]
        actions = [(row["action"], row["outcome"]) for row in review_store.audit]
        assert actions == [("merge_refused", "refused")]

    async def test_proxied_host_never_reaches_merge(
        self, proxied_client, fleet, review_store
    ):
        response = await proxied_client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "local_only"
        assert review_store.audit[-1]["action"] == "merge_refused"

    async def test_forwarding_header_never_reaches_merge(
        self, client, fleet, review_store
    ):
        response = await client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
            headers={"x-forwarded-for": "203.0.113.7"},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "local_only"
        assert review_store.audit[-1]["action"] == "merge_refused"

    async def test_explicitly_granted_local_host_passes_the_gate(
        self, review_store, feature_on, fleet, monkeypatch
    ):
        from httpx import ASGITransport, AsyncClient

        from src.server.app.review_merge import router
        from src.server.services.autonomy import review_merge as service
        from tests.conftest import create_test_app

        monkeypatch.setenv(
            "CONDUCTOR_MERGE_LOCAL_HOSTS", "127.0.0.1,conductor.local"
        )
        commit_file(fleet.line, "work.txt", "work\n", "line work")
        monkeypatch.setattr(
            service, "_run_codex_merge_review", lambda **kwargs: "merge"
        )
        app = create_test_app(router)
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 5555)),
            base_url="http://conductor.local",
        ) as http:
            response = await http.post(
                MERGES,
                json={
                    "line": "line",
                    "into": "integrate/r2",
                    "test_cmd": "true",
                    "root": str(fleet.root),
                },
            )

        # The local gate accepted the request and the merge proceeded through
        # the rehearsal and the (scripted) Codex review.
        assert response.status_code == 200, response.text
        assert response.json()["codex_verdict"] == "merge"

    async def test_merge_gate_is_the_codex_review_the_server_runs(
        self, client, fleet, monkeypatch
    ):
        from src.server.services.autonomy import review_merge as service

        commit_file(fleet.line, "work.txt", "work\n", "line work")
        monkeypatch.setattr(
            service, "_run_codex_merge_review", lambda **kwargs: "merge"
        )

        response = await client.post(
            MERGES,
            json={
                "line": "line",
                "into": "integrate/r2",
                "test_cmd": "true",
                "root": str(fleet.root),
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["merged"] is True

    async def test_a_codex_block_refuses_the_merge(
        self, client, fleet, review_store, monkeypatch
    ):
        from src.server.services.autonomy import review_merge as service

        commit_file(fleet.line, "work.txt", "work\n", "line work")
        before = head(fleet.target)

        def blocked(**kwargs):
            raise service.ReviewMergeError(
                "codex_blocked",
                "The Codex pre-merge review returned BLOCK; the target branch was not touched.",
                remediation="Address the findings and retry.",
                status_code=409,
                kind="refused",
            )

        monkeypatch.setattr(service, "_run_codex_merge_review", blocked)
        response = await client.post(
            MERGES,
            json={
                "line": "line",
                "into": "integrate/r2",
                "test_cmd": "true",
                "root": str(fleet.root),
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "codex_blocked"
        assert head(fleet.target) == before
        assert review_store.audit[-1]["action"] == "merge_refused"

    async def test_merge_refuses_a_dirty_target(self, client, fleet):
        (fleet.target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

        response = await client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "target_dirty"

    async def test_unknown_target_branch_has_remediation(self, client, fleet):
        response = await client.post(
            MERGES,
            json={"line": "line", "into": "integrate/missing", "root": str(fleet.root)},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "target_worktree_missing"
        assert "worktree" in detail["remediation"]


class TestAuditList:
    async def test_audit_list_returns_refusals_and_filters(self, client, fleet):
        (fleet.target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        await client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
        )

        response = await client.get(AUDIT, params={"line": "line", "action": "merge_refused"})

        assert response.status_code == 200
        rows = response.json()["audit"]
        assert len(rows) == 1
        assert rows[0]["action"] == "merge_refused"
        assert rows[0]["outcome"] == "refused"
        assert rows[0]["target_branch"] == "integrate/r2"
        assert rows[0]["detail"]["code"] == "target_dirty"

    async def test_audit_list_rejects_an_unknown_action(self, client, fleet):
        response = await client.get(AUDIT, params={"action": "nonsense"})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_audit_action"


class TestFeatureGate:
    async def test_review_routes_are_hidden_without_the_super_agent_feature(
        self, client, fleet, monkeypatch
    ):
        from src.server.services import features

        async def disabled(user_id: str, key: str) -> bool:
            return False

        monkeypatch.setattr(features, "user_feature_enabled", disabled)

        review = await client.post(
            REVIEWS,
            json={"line": "line", "reviewer": "repo", "root": str(fleet.root)},
        )
        merge = await client.post(
            MERGES,
            json={"line": "line", "into": "integrate/r2", "root": str(fleet.root)},
        )

        assert review.status_code == 404
        assert merge.status_code == 404


SECRET = "Zq8mW3xLp0vTn5Rk"


class TestRefusalsDoNotReadTheCallersTextBack:
    """The review routes are authenticated and remote-available, so a name,
    action or root the request chose is not repeated in the refusal: a
    credential pasted into one would come back. (The merge route is loopback
    only and names its refs.) The server's own configured root is named, since
    that is what a client has to match."""

    async def test_an_unknown_audit_action_is_not_named(self, client):
        response = await client.get(AUDIT, params={"action": SECRET})

        assert response.status_code == 400
        assert SECRET not in response.text
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_audit_action"
        assert "merge_refused" in detail["remediation"]  # the options are the help

    @pytest.mark.parametrize(
        "line, reviewer, field",
        [
            (SECRET, "repo", "line"),  # a name that is not a worktree
            (f"{SECRET}/x", "repo", "line"),  # a name that is a path
            ("line", SECRET, "reviewer"),
            ("line", f"..{SECRET}", "reviewer"),
            ("line", "x" * 201, "reviewer"),
        ],
    )
    async def test_an_unknown_line_or_reviewer_is_named_by_its_field(
        self, client, fleet, line, reviewer, field
    ):
        response = await client.post(
            REVIEWS,
            json={"line": line, "reviewer": reviewer, "root": str(fleet.root)},
        )

        # A name over 200 characters never reaches the lookup: it is a 422.
        assert response.status_code in (404, 422)
        assert SECRET not in response.text
        if response.status_code == 404:
            detail = response.json()["detail"]
            assert detail["code"] == "line_not_found"
            assert detail["message"].startswith(f"Unknown {field}")

    async def test_a_root_that_resolves_elsewhere_is_not_followed_into_the_answer(
        self, client, fleet, tmp_path
    ):
        elsewhere = tmp_path / f"{SECRET}-target"
        elsewhere.mkdir()
        (tmp_path / "alias").symlink_to(elsewhere)

        response = await client.post(
            REVIEWS,
            json={"line": "line", "reviewer": "repo", "root": str(tmp_path / "alias")},
        )

        assert response.status_code == 409
        assert SECRET not in response.text
        detail = response.json()["detail"]
        assert detail["code"] == "worktree_root_mismatch"
        assert detail["detail"] == {"server_root": str(fleet.root.resolve())}
        assert str(fleet.root.resolve()) in detail["message"]

    @pytest.mark.parametrize("root", ["~nosuchuser_zz/x", "a\x00b"])
    async def test_a_root_that_cannot_be_resolved_is_a_400_and_not_a_500(
        self, client, fleet, root
    ):
        response = await client.post(
            REVIEWS, json={"line": "line", "reviewer": "repo", "root": root}
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "worktree_root_invalid"
        assert "nosuchuser_zz" not in response.text

    async def test_the_oserror_that_quotes_a_path_is_not_passed_on(
        self, fleet, monkeypatch
    ):
        from pathlib import Path

        from src.server.services.autonomy import review_merge as service

        real = Path.resolve

        def refuse(self, *args, **kwargs):
            if self.name == "claimed":
                raise OSError(f"[Errno 40] Too many levels of symbolic links: '{SECRET}'")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", refuse)
        with pytest.raises(service.ReviewMergeError) as error:
            service.resolve_request_root("/somewhere/claimed")
        assert error.value.code == "worktree_root_invalid"
        assert SECRET not in str(error.value.as_detail())

    async def test_a_line_that_cannot_be_resolved_does_not_carry_the_oserror(
        self, fleet, monkeypatch
    ):
        from pathlib import Path

        from src.server.services.autonomy import review_merge as service

        def refuse(self, *args, **kwargs):
            raise OSError(f"[Errno 63] File name too long: '{SECRET}'")

        monkeypatch.setattr(Path, "resolve", refuse)
        with pytest.raises(service.ReviewMergeError) as error:
            service.resolve_line(fleet.root, "line", field="reviewer")
        assert error.value.code == "line_not_found"
        assert SECRET not in str(error.value.as_detail())
        assert error.value.message.startswith("The reviewer cannot be resolved")
