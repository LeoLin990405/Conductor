"""The local OSS updater: fast-forward only, clean tree required, no network in tests."""

from src.server.services import local_updater


def test_status_reports_checkout_identity(monkeypatch):
    monkeypatch.setattr(local_updater, "_is_checkout", lambda: True)
    monkeypatch.setattr(local_updater, "_enabled", lambda: True)
    monkeypatch.setattr(local_updater, "_version", lambda: "9.9.9")
    outputs = {
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-parse", "--short", "HEAD"): "abc1234",
        ("status", "--porcelain"): "",
        ("remote", "get-url", "origin"): "https://example.test/repo",
    }
    monkeypatch.setattr(local_updater, "_git", lambda *args, **_kwargs: outputs[tuple(args)])
    assert local_updater.status() == {
        "supported": True,
        "enabled": True,
        "version": "9.9.9",
        "commit": "abc1234",
        "branch": "main",
        "dirty": False,
        "remote": "https://example.test/repo",
    }


def test_check_reports_the_commits_behind(monkeypatch):
    monkeypatch.setattr(local_updater, "_is_checkout", lambda: True)
    values = {
        ("status", "--porcelain"): "",
        ("fetch", "origin", "main"): "",
        ("rev-parse", "--short", "HEAD"): "old",
        ("rev-parse", "--short", "origin/main"): "new",
        ("rev-list", "--count", "HEAD..origin/main"): "2",
        ("rev-list", "--count", "origin/main..HEAD"): "0",
        ("log", "--oneline", "--max-count=20", "HEAD..origin/main"): "new one\nnew two",
    }
    monkeypatch.setattr(local_updater, "_git", lambda *args, **_kwargs: values[tuple(args)])
    result = local_updater.check()
    assert result["update_available"] is True
    assert result["behind"] == 2
    assert result["commits"] == ["new one", "new two"]


def test_apply_refuses_a_dirty_tree(monkeypatch):
    monkeypatch.setattr(local_updater, "_is_checkout", lambda: True)
    monkeypatch.setattr(local_updater, "_enabled", lambda: True)
    monkeypatch.setattr(local_updater, "_git", lambda *args, **_kwargs: " M web/src/file.ts")
    try:
        local_updater.apply()
    except local_updater.LocalUpdaterError as exc:
        assert "local changes" in str(exc)
    else:
        raise AssertionError("dirty tree must be refused")


def test_apply_fast_forwards_and_rebuilds(monkeypatch):
    monkeypatch.setattr(local_updater, "_is_checkout", lambda: True)
    monkeypatch.setattr(local_updater, "_enabled", lambda: True)
    head_reads = 0

    def fake_git(*args, **_kwargs):
        nonlocal head_reads
        if tuple(args) == ("rev-parse", "--short", "HEAD"):
            head_reads += 1
            return "old" if head_reads == 1 else "new"
        if tuple(args) == ("status", "--porcelain"):
            return ""
        return "ok"

    monkeypatch.setattr(local_updater, "_git", fake_git)
    monkeypatch.setattr(local_updater, "check", lambda: {
        "update_available": True,
        "current": "old",
    })
    steps: list[str] = []
    monkeypatch.setattr(local_updater, "_run_step", lambda label, command, timeout: steps.append(label))
    result = local_updater.apply()
    assert result["updated"] is True
    assert result["old_commit"] == "old"
    assert result["new_commit"] == "new"
    assert steps == ["uv sync", "pnpm install", "web build"]
