# XiaoTie repo map + LSP (absorbed reference)

Source: `LeoLin990405/xiaotie` @ `d9ae321` (2026-11-04), MIT.

Absorbed:

- `repomap_v2.py` — tree-sitter + NetworkX PageRank repository map with a
  regex fallback when optional parsers are unavailable.
- `lsp/client.py`, `lsp/protocol.py`, `lsp/manager.py` — standalone LSP
  client/manager/protocol. `diagnostics.py` was not copied because it depends on
  XiaoTie's tool base; the client/manager are independent.
- `tests/test_repomap_v2.py`, `tests/test_lsp_protocol.py` with imports pointed
  at `contrib.xiaotie`.

This is a **reference absorb**, not wired into Conductor's runtime yet. The
source MIT license is copied unchanged. TUI, proxy, Telegram, scraper,
automation, onboarding, and secret-manager code were not copied.

Run:

```bash
pytest contrib/xiaotie/tests -q
```
