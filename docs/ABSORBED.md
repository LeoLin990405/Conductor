# Absorbed repositories

This file records code moved into Conductor from repositories that are now
archived. Only the listed modules were copied; the original repositories remain
read-only archives and were not deleted.

| Source repository | Source commit | License | Absorbed into | Notes |
|---|---|---|---|---|
| `LeoLin990405/agos` | `ac22d67` | MIT | `contrib/agos-router/`, `contrib/dsh-agos/` | Router outcome/ledger/shadow audit logic + MIT license (in `contrib/agos-router/`); `contrib/dsh-agos/lib/secrets-gate.js` is the redaction classifier from `plugins/dsh-agos/lib`, added in the same commit `125ffb35e` (see `contrib/agos-router/README.md`); frontend/DSH host code not copied |
| `LeoLin990405/mmteam-cc` | `a42a5db` | Apache-2.0 | `contrib/mmteam/` | Standalone A2A/MCP bridge scripts + command docs + LICENSE/NOTICE; not wired into runtime |
| `private-upstream/model-farm-handoff` | `be8240f` | No upstream LICENSE (owner-authored subset) | `contrib/model_farm/` | `decision_router.py`, `placement.py` and fixture/eval data only; no NAS/private fixtures |
| `LeoLin990405/xiaotie` | `d9ae321` | MIT | `contrib/xiaotie/` | `repomap_v2.py` and LSP client/protocol/manager + MIT license; TUI/proxy/telegram excluded |

Archived without code absorption:

| Repository | Commit at archive | Reason |
|---|---|---|
| `private-upstream/model-farm-ledger` | `298ef5f` | Mirror/README only; no reusable implementation |
| `private-upstream/dsh-plugins` | `9748039` | DSH desktop UI/vendored plugins; not applicable to Conductor's backend/CLI |

Absorbed code is reference material under `contrib/` and is not imported by the
Conductor server or CLI unless separately promoted in a reviewed change. Tests
and import-path adaptations are recorded in each `contrib/*/README.md`.

No `.env`, credential, token, or candidate-secret fixture was copied.
