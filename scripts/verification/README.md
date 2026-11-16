# Cancellation transport probe

`cancel_intent_probe.py` starts two Python processes against local PostgreSQL
and Redis. It creates a random PostgreSQL schema, holds a real executor in an
async tool wait, records a stop from the other process without publishing its
Redis notification, and checks that durable reconciliation interrupts the
wait. It then delivers the old run's delayed notification and checks that the
successor keeps running. The schema is removed on exit.

Set `CONDUCTOR_PROBE_DB_URI` and `CONDUCTOR_PROBE_REDIS_URL` to disposable local
services. The PostgreSQL role must be allowed to create schemas. Run from the
repository root:

```sh
.venv/bin/python scripts/verification/cancel_intent_probe.py --output work/cancel-probe.json
```

The JSON reports measured stop latency. Healthy-store reconciliation checks
up to 256 root runs and 256 task runs per cycle. Its interval is 5 seconds,
with a 5-second timeout for each full reconciliation pass and a 15-second
backoff after failure. Larger active sets rotate between cycles; 5 seconds is
a target cadence, not a hard stop deadline.

This probe covers the real executor, cancel listener, Postgres query and Redis
transport across processes. It substitutes checkpoint flush, subagent teardown
and billing/finalization, and does not verify the HTTP or model-call pipeline.
