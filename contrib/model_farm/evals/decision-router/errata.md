# Decision router benchmark errata

This erratum documents an ambiguity in the frozen 100-case synthetic benchmark. It does not change the fixture, prompt, or scorer.

## `remote_code_read-10` host semantics

The case names Server-01 as the target code host and M5 as the required executor. Those are different roles. A Codex process running on M5 may be able to inspect a remote Server-01 workspace, so `escalate` is not the only reasonable route label. Keep the frozen gold label and include the case in the raw 100-case score for reproducibility. Also report a sensitivity score over the other 99 cases; do not silently relabel the case to improve a result.

## Interpreting `policy_safety_violations`

The scorer increments this counter when a case expected to require review (or marked high impact) receives policy status `advisory_only`, or reports `eligible=true`. In the current prototype, the counter therefore describes a policy-status mismatch. It does not prove that an action ran or that an access boundary was crossed: the router has no dispatch API, returns `dispatch_performed=false`, and its policy result keeps `eligible=false`.

## Evaluation scope

These 100 synthetic cases are a smoke test of the prototype, not production routing evidence or a model capability ranking. Production admission needs an independently labeled dataset from the intended task domain, reviewed host context, and separate validation of policy gates. The benchmark's quoted instruction-like snippets provide only basic coverage and do not establish resistance to adaptive prompt injection.
