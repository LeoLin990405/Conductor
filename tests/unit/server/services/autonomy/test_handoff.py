"""Handoff manifests: confinement, secrets, checksums, all-or-nothing."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.server.services.autonomy import handoff as svc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "draft.md").write_bytes(b"draft body\n")
    (tmp_path / "data.csv").write_bytes(b"a,b\n1,2\n")
    return tmp_path


class TestConfinePath:
    def test_inside_path_resolves(self, root: Path) -> None:
        target = svc.confine_path(root, "out/draft.md")
        assert target == (root / "out" / "draft.md").resolve()

    def test_dotdot_is_refused_by_shape(self, root: Path) -> None:
        with pytest.raises(svc.PathEscapeError):
            svc.confine_path(root, "../outside.md")

    def test_absolute_path_is_refused(self, root: Path) -> None:
        with pytest.raises(svc.PathEscapeError):
            svc.confine_path(root, "/etc/passwd")

    def test_symlink_escape_is_refused(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside-handoff-target.txt"
        outside.write_bytes(b"secret")
        try:
            (root / "alias.md").symlink_to(outside)
            with pytest.raises(svc.PathEscapeError):
                svc.confine_path(root, "alias.md")
        finally:
            outside.unlink(missing_ok=True)

    def test_backslash_is_not_a_separator_escape(self, root: Path) -> None:
        with pytest.raises(svc.PathEscapeError):
            svc.confine_path(root, "..\\..\\etc")

    @pytest.mark.parametrize(
        "rel",
        [
            ".env",
            "config/settings.env",
            "secrets.json",
            "creds/api_key.txt",
            "keys/id_rsa",
            "server.pem",
            "vault.kdbx",
            ".git/config",
            "node_modules/pkg/index.js",
            "auth-token.txt",
        ],
    )
    def test_secret_looking_paths_are_refused(self, root: Path, rel: str) -> None:
        with pytest.raises(svc.SecretPathError):
            svc.confine_path(root, rel)

    @pytest.mark.parametrize(
        "rel",
        ["report.md", "out/draft.md", "data/2026/summary.csv"],
    )
    def test_normal_paths_pass(self, root: Path, rel: str) -> None:
        assert svc.confine_path(root, rel) == (root / rel).resolve()


class TestSecretPattern:
    @pytest.mark.parametrize(
        "name",
        [
            "id_rsa",
            "id_ed25519",
            "authorized_keys",
            "prod.pem",
            "service.key",
            "backup.kdbx",
            ".env",
            "deploy.env",
            "token.txt",
            "client_secret.json",
            "my-password.bin",
            "credentials",
        ],
    )
    def test_secret_names(self, name: str) -> None:
        assert svc.secret_looking_path(f"dir/{name}")

    @pytest.mark.parametrize(
        "name",
        [
            "notes.md",
            "summary.txt",
            "tokenizer.py",
            "key-findings.md",
            "tokenized-report.md",
            "totally-fine.csv",
        ],
    )
    def test_benign_names(self, name: str) -> None:
        assert not svc.secret_looking_path(f"dir/{name}")


class TestManifestEntry:
    def test_entry_has_size_and_sha(self, root: Path) -> None:
        entry = svc.build_manifest_entry(root, "out/draft.md")
        assert entry == {
            "path": "out/draft.md",
            "size": len(b"draft body\n"),
            "sha256": _sha(b"draft body\n"),
        }

    def test_missing_file_is_refused(self, root: Path) -> None:
        with pytest.raises(svc.MissingFileError):
            svc.build_manifest_entry(root, "out/absent.md")

    def test_directory_is_refused(self, root: Path) -> None:
        with pytest.raises(svc.MissingFileError):
            svc.build_manifest_entry(root, "out")

    def test_symlink_entry_is_refused(self, root: Path) -> None:
        (root / "link.md").symlink_to(root / "data.csv")
        with pytest.raises(svc.PathEscapeError):
            svc.build_manifest_entry(root, "link.md")


class TestValidateManifest:
    def _files(self, path: str = "a.md") -> list[dict]:
        return [{"path": path, "size": 3, "sha256": "a" * 64}]

    def test_minimal_manifest(self) -> None:
        files = svc.validate_manifest(
            self._files(), purpose="p", producer="w", consumer="r"
        )
        assert files == [{"path": "a.md", "size": 3, "sha256": "a" * 64}]

    def test_empty_manifest_is_refused(self) -> None:
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest([], purpose="p", producer="w", consumer="r")

    def test_missing_actor_is_refused(self) -> None:
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(self._files(), purpose="p", producer="", consumer="r")

    def test_bad_sha_is_refused(self) -> None:
        bad = [{"path": "a.md", "size": 3, "sha256": "XYZ"}]
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(bad, purpose="p", producer="w", consumer="r")

    def test_negative_size_is_refused(self) -> None:
        bad = [{"path": "a.md", "size": -1, "sha256": "a" * 64}]
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(bad, purpose="p", producer="w", consumer="r")

    def test_duplicate_path_is_refused(self) -> None:
        dup = self._files() + self._files()
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(dup, purpose="p", producer="w", consumer="r")

    def test_file_cap_is_enforced(self) -> None:
        many = [{"path": f"f{i}.md", "size": 1, "sha256": "a" * 64} for i in range(257)]
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(many, purpose="p", producer="w", consumer="r")

    def test_purpose_cap_is_enforced(self) -> None:
        with pytest.raises(svc.ManifestError):
            svc.validate_manifest(
                self._files(), purpose="x" * 501, producer="w", consumer="r"
            )

    def test_secret_path_in_supplied_manifest_is_refused(self) -> None:
        bad = self._files(".env")
        with pytest.raises(svc.SecretPathError):
            svc.validate_manifest(bad, purpose="p", producer="w", consumer="r")


class FakeHandoffStore:
    """In-memory stand-in for the handoffs db module."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.settled: list[tuple[str, str, str]] = []
        self._seq = 0

    async def create_handoff(
        self, *, ticket_id, user_id, producer, consumer, purpose, files
    ):
        self._seq += 1
        row = {
            "handoff_id": f"h{self._seq}",
            "ticket_id": ticket_id,
            "user_id": user_id,
            "producer": producer,
            "consumer": consumer,
            "purpose": purpose,
            "status": "registered",
            "files": files,
            "verify_error": "",
            "verified_at": None,
        }
        self.rows[row["handoff_id"]] = row
        return dict(row)

    async def settle_handoff(
        self,
        handoff_id: str,
        *,
        status: str,
        verify_error: str = "",
        expected_status: str = "registered",
    ):
        row = self.rows.get(handoff_id)
        if row is None or row["status"] != expected_status:
            return None
        row["status"] = status
        row["verify_error"] = verify_error
        self.settled.append((handoff_id, status, verify_error))
        return dict(row)


@pytest.fixture
def store(monkeypatch) -> FakeHandoffStore:
    fake = FakeHandoffStore()
    monkeypatch.setattr(svc.handoff_db, "create_handoff", fake.create_handoff)
    monkeypatch.setattr(svc.handoff_db, "settle_handoff", fake.settle_handoff)
    return fake


class TestRegisterHandoff:
    @pytest.mark.asyncio
    async def test_register_stores_manifest(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="worker:glm",
            consumer="deliberation:summarizer",
            purpose="draft sections",
            paths=["out/draft.md", "data.csv"],
        )
        assert row["status"] == "registered"
        assert [f["path"] for f in row["files"]] == ["out/draft.md", "data.csv"]
        for entry in row["files"]:
            assert entry["sha256"] == _sha((root / entry["path"]).read_bytes())

    @pytest.mark.asyncio
    async def test_bad_path_refuses_whole_handoff(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        with pytest.raises(svc.SecretPathError):
            await svc.register_handoff(
                root,
                ticket_id="t1",
                user_id="u1",
                producer="a",
                consumer="b",
                purpose="",
                paths=["out/draft.md", ".env"],
            )
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_missing_file_refuses_whole_handoff(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        with pytest.raises(svc.MissingFileError):
            await svc.register_handoff(
                root,
                ticket_id="t1",
                user_id="u1",
                producer="a",
                consumer="b",
                purpose="",
                paths=["out/draft.md", "gone.md"],
            )
        assert store.rows == {}

    @pytest.mark.asyncio
    async def test_escape_refuses_whole_handoff(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        with pytest.raises(svc.PathEscapeError):
            await svc.register_handoff(
                root,
                ticket_id="t1",
                user_id="u1",
                producer="a",
                consumer="b",
                purpose="",
                paths=["../draft.md"],
            )
        assert store.rows == {}


class TestVerifyHandoff:
    @pytest.mark.asyncio
    async def test_untouched_files_verify(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md"],
        )
        settled = await svc.verify_handoff(root, row)
        assert settled["status"] == "verified"
        assert settled["verify_error"] == ""

    @pytest.mark.asyncio
    async def test_tampered_file_rejects(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md", "data.csv"],
        )
        # Same length, different bytes: only the checksum can catch it.
        (root / "out" / "draft.md").write_bytes(b"DRAFT BODY\n")
        settled = await svc.verify_handoff(root, row)
        assert settled["status"] == "rejected"
        assert "sha256 mismatch" in settled["verify_error"]
        assert "out/draft.md" in settled["verify_error"]

    @pytest.mark.asyncio
    async def test_missing_file_rejects(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md"],
        )
        (root / "out" / "draft.md").unlink()
        settled = await svc.verify_handoff(root, row)
        assert settled["status"] == "rejected"
        assert "not a file" in settled["verify_error"]

    @pytest.mark.asyncio
    async def test_resized_file_rejects_on_size(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md"],
        )
        (root / "out" / "draft.md").write_bytes(b"x")
        settled = await svc.verify_handoff(root, row)
        assert settled["status"] == "rejected"
        assert "size mismatch" in settled["verify_error"]

    @pytest.mark.asyncio
    async def test_replacement_symlink_rejects(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md"],
        )
        (root / "out" / "draft.md").unlink()
        (root / "out" / "draft.md").symlink_to(root / "data.csv")
        settled = await svc.verify_handoff(root, row)
        assert settled["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_settled_row_never_flips(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        row = await svc.register_handoff(
            root,
            ticket_id="t1",
            user_id="u1",
            producer="a",
            consumer="b",
            purpose="",
            paths=["out/draft.md"],
        )
        first = await svc.verify_handoff(root, row)
        (root / "out" / "draft.md").write_bytes(b"tampered\n")
        second = await svc.verify_handoff(root, first)
        assert second["status"] == "verified"
        assert len(store.settled) == 1

    @pytest.mark.asyncio
    async def test_row_without_id_is_refused(
        self, root: Path, store: FakeHandoffStore
    ) -> None:
        with pytest.raises(svc.ManifestError):
            await svc.verify_handoff(root, {"status": "registered"})

    @pytest.mark.asyncio
    async def test_large_file_hashes_streamed(
        self, root: Path, store: FakeHandoffStore, monkeypatch
    ) -> None:
        big = root / "big.bin"
        payload = b"\x00" * (svc._HASH_CHUNK * 3 + 17)
        big.write_bytes(payload)
        entry = svc.build_manifest_entry(root, "big.bin")
        assert entry["sha256"] == _sha(payload)
        assert entry["size"] == len(payload)
