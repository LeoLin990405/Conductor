# ob-switch — switch a stuck line to another model

Self-iteration G7. Clean-room: designed from the one-line idea only; no external code consulted.

## Goal
A **line** is one Ticket's Worker run (provider + model + worktree/worker thread). When it is stuck, move it to a
different provider/model **in the same worktree**, carry context in a compact handoff, and record the switch on the
Ticket. The replacement is never the line's reviewer model.

Stuck means any of: no progress for `stall_seconds`; `max_failures` consecutive failures; the current provider's
breaker is open (see the internal document `provider-breaker` (not published)).

## Data model
- Migration 125 (after head 124): `autonomy_tickets.switches JSONB NOT NULL DEFAULT '[]'`; downgrade drops it.
  Each entry: `{at, reason, from:{provider,model}, to:{provider,model}, handoff_chars, attempt}`.
- Reviewer: `mandate.reviewer = {provider, model}` (absent = no exclusion beyond the current model).
- Handoff (`Handoff` dataclass, pure): `summary`, `diff`, `findings[]`, rendered to a bounded text block.

## Modules
- `src/server/services/autonomy/line_switch.py` (pure, no I/O):
  `SwitchPolicy`, `detect_stuck(line, policy) -> reason|None`, `pick_target(candidates, current, reviewer, open_providers)`,
  `build_handoff(messages, diff, findings, limit)`, `inject_handoff(prompt, handoff)`, `plan_switch(...) -> SwitchPlan`.
  Selection is deterministic (candidate order = preference), skips the current model, the reviewer (same provider+model),
  and providers whose breaker is open.
- `tickets_db.record_switch(...)`: appends to `switches` and updates `worker_provider/worker_model` with a status fence.
- API `POST /api/v1/autonomy/tickets/{id}/switch` `{to_provider?, to_model?, reason?, candidates?}`: manual or automatic
  switch; 409 if no eligible target.
- CLI `conductor tickets switch <id> [--provider P --model M] [--reason R]`.

## Non-goals
No real provider calls; automatic triggering from the watchdog is left as the follow-up (the pure `detect_stuck` is the hook).

## Tests (fakes only)
stuck detection (stall / failures / breaker), target pick (skips current, reviewer, open breakers, explicit target equal to
reviewer refused), handoff bounds and prompt injection, plan/record with a fake store, CLI parse + request, migration
chain/reversibility.

## Gap mapping
| Idea clause | Where |
|---|---|
| stuck detection | `detect_stuck` |
| same worktree | switch changes only provider/model; worker thread/worktree untouched |
| handoff summary, diff, findings | `build_handoff` / `inject_handoff` |
| record on Ticket | migration 125 + `record_switch` |
| never reviewer | `pick_target` + API validation |
