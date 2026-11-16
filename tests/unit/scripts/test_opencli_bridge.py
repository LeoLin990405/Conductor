"""The OpenCLI sidecar only accepts the two news commands."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("opencli_bridge", ROOT / "scripts/opencli_bridge.py")
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


def test_allows_only_news_commands():
    assert bridge._allowed(["xueqiu", "hot", "--limit", "5"])
    assert bridge._allowed(["weixin", "search", "财联社", "--limit", "10"])
    assert not bridge._allowed(["xueqiu", "login"])
    assert not bridge._allowed(["browser", "open", "https://example.com"])
    assert not bridge._allowed(["xueqiu", "hot", "--bogus"])
    assert not bridge._allowed(["xueqiu"])
