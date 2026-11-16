# model-farm decision router (absorbed reference)

Source: `private-upstream/model-farm-handoff` @ `be8240f` (2026-11-04).

This private repository has **no LICENSE**. Only owner-authored,
dependency-free routing/placement code is absorbed here; all other code remains
in the source repository. The router is a local-only advisory classifier with
strict model-digest allowlisting, byte/time caps, and a fail-closed placement
policy. It is not wired into Conductor's runtime.

Absorbed files:

- `decision_router.py`
- `placement.py` + `config/executor-placement.json` (the fail-closed placement
  policy it needs)
- `evals/decision-router/` (models, cases, labels, errata)
- `tests/test_decision_router.py` (import/paths adjusted for this contrib layout)

Paths in `MODEL_REGISTRY_PATH` and `POLICY_PATH` were adapted to this directory.
No `.env`, credentials, farm-staging fixtures, or NAS runtime code were copied.

Run:

```bash
pytest contrib/model_farm/tests/test_decision_router.py -q
```
