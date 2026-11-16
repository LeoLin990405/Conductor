# Super-agent offline experiments

Two reproducible offline harnesses now cover the article's §7.1 ablation and §7.2 Router batching mechanisms. They are new fake-backed experiments, not a wholesale port of legacy code. Each uses a hard $0 model budget and makes no provider, backend, database, network, or credential calls.

[`scripts/superagent_ablation.py`](../../scripts/superagent_ablation.py) runs four matched synthetic policies over five paired seeds: 20 runs, 6,000 events, and 400 tasks. It exercises `src/server/services/autonomy/intake.receive_trigger` with in-memory persistence fakes. [`scripts/superagent_router_stress.py`](../../scripts/superagent_router_stress.py) runs five replicates at six arrival rates (1, 5, 10, 20, 40, and 160 per minute): 60 cases and 6,043 paired arrivals across batched and unbatched arms. It exercises production Eval scheduling, effect routing, and Router batch dispatch with virtual model/service timings.

The new harnesses comprise two Python source files, two focused unit-test files, two experiment READMEs, and saved JSON/JSONL results. Router raw ledgers use gzip compression. Each experiment README documents assumptions, commands, measures, and evidence limits: ablation and Router stress.

Source context includes the local article strip at `/Users/you/conductor-wt/ref/superagent.html`, existing [`scripts/superagent_pressure.py`](../../scripts/superagent_pressure.py), and production `src/server/services/autonomy/intake.py`, `evals.py`, `router.py`, and `policy.py`. The current alignment count is 25 Aligned, 10 Partial, and 0 Missing. These two Aligned rows mean the named mechanisms ran in offline simulations; they do not establish prompt quality, live model performance, token/cache savings, or production capacity.

The final combined focused validation passed 77 tests in 2.01 seconds across both harness suites, related autonomy contracts, and the alignment count check. The exact command, file hashes, validation evidence, rollback, and local-only publication status are in the change record. Fleet Memory/NAS publication remains pending because this task forbids network access.
