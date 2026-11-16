# dispatch_gates_v1 fixture

`dispatch_gates_v1.json` is one full `GET /api/v1/dispatch/{id}` response
(`dispatch_summary()`) for a dispatch parked at G6 with one risk-bearing line.
W3 produces this shape, W5 renders it. Contract: the internal planning note `03-grok` (not published),
section "gate JSON 契约：`dispatch_gates_v1`".

## Outer fields vs `machine`

Per line under `gates.lines.<line_id>`:

| Part | Owner | Vocabulary |
|---|---|---|
| `phase`, `author`, `*_reviewer`, `fix_round`, `blocking_rounds`, `replans`, `reason`, `gates.*`, `history`, `handoff_ref` | dispatch (W3 `gates.py`) | dispatch-level words: review `verdict` is `merge` / `block`, as the API exposes |
| `events` | Stage-Gate | `GateEvent.to_dict()` of every accepted transition (latest 50) |
| `machine` | Stage-Gate | exactly `StageGateState.to_dict()`, with `handoff` set to `null` (only `handoff_ref` kept) |

`machine` keeps Stage-Gate's internal values. A risk-bearing primary pass is
`primary_verdict: "approve"`; Stage-Gate refuses a primary `merge` on a
risk-bearing line (`secondary_review_required`).

## MERGE / BLOCK mapping

The dispatch reviewer says MERGE or BLOCK. Mapping that to Stage-Gate verdicts
(MERGE -> `approve`, BLOCK -> `block`) is owned by W3's adapter in
`src/tools/auto_dispatch/gates.py`, including whether a non-risk line sends
Stage-Gate's `merge` (which skips the secondary review). W0 only documents the
two vocabularies.

## `fix_round` vs `fix_rounds`

- Outer `fix_round`: how many fix rounds the dispatch launched for the line.
  It only grows (here `1`: one BLOCK, one fix).
- Inner `machine.fix_rounds` / `machine.blocking_rounds`: Stage-Gate's own
  counters for the current reviewer's round sequence. A passing review or a
  changed reviewer resets both, so after the fix was approved they are `0`,
  while `machine.return_stage` still says `primary_review`.

## Top level

`spec_ref` is the SPEC archive reference (`Dispatch.spec_ref`), an opaque
string for W5; W4 defines its format.

## Regenerating

`machine` and `events` are generated, never hand-edited:

```bash
.venv/bin/python tests/fixtures/gen_dispatch_gates_v1.py          # rewrite
.venv/bin/python tests/fixtures/gen_dispatch_gates_v1.py --check  # drift check
```

The generator drives `src/server/services/autonomy/stage_gate.py` from
Stage-Gate source commit `aee455e00` (merged into integrate/r2 at `8de76397d`)
through: planning -> G0 -> D1 assign -> implement -> G2 -> primary BLOCK ->
fix -> primary APPROVE -> secondary APPROVE -> G5 -> `awaiting_human` (G6),
with fixed timestamps. If a Stage-Gate rule change makes a step refuse, the
generator fails; fix the scenario or the contract, not the rule.
`tests/unit/tools/auto_dispatch/test_dispatch_gates_fixture.py` compares the
fixture with the generator field for field.
