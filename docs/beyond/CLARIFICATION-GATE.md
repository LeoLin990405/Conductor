# Clarification Gate

Status: design. Implemented behind `AUTONOMY_CLARIFY_ENABLED` (default off) in
`src/server/services/autonomy/clarify.py`; storage in `autonomy_ticket_clarifications`
(migration 111 also widens the `autonomy_tickets.status` CHECK constraint).

## Problem

Vague prompts make autonomous workers guess. A worker launched on
"do something with the repo asap" burns a worker slot, model tokens, and often a
channel round-trip before anyone discovers the goal was wrong — and a wrong guess
that ships a deliverable is worse than one that stalls. Clarifying *after* launch
is the expensive path: the slot is occupied, progress made on a wrong premise is
discarded, and every retry re-pays the cost. Clarifying *before* launch costs one
cheap scoring pass and one question on the channel the request already came in on.

## Why it's hard

- **Clarity is subjective.** Any threshold trades false positives (an annoying
  "did you mean?" on a perfectly fine prompt) against silent failure (launching a
  guessing worker). There is no setting that is right for every source and user.
- **Three intake paths.** The gate must sit in the web UI (manual tasks), Feishu,
  and GitHub (host route accept branch) without breaking idempotent ticket
  creation — a retried webhook must not create a second ticket or a second draft.
- **Two confirmation surfaces.** A confirm can arrive as a UI POST or as a reply
  on the originating channel (intake), and both must resume the ticket exactly
  once even when they race each other.
- **Multi-worker safety.** The confirm request and the channel intake may be
  served by different backend processes; nothing on the resume path may live in
  process memory.
- **Scheduler hygiene.** The gate state must not starve the worker-slot
  scheduler (which only launches `queued` tickets) and must not trip the stall
  watchdog into attention spam.
- **Scoring seam.** LLM scoring needs a mockable protocol and a no-LLM fallback,
  because many deployments run without usable background-model credentials.

## Design

### State machine

```
queued -> awaiting_clarification -> queued -> running -> ...
                |                          ^
                +------ confirm ----------+      (cancel from any state)
```

A ticket is created `queued` as today; the gate runs after creation, before any
launch effect is enqueued. Below threshold, status flips to
`awaiting_clarification` and the ticket simply waits — no worker, no slot held.
A confirm flips it back to `queued`, and the ordinary launch path picks it up.

### Scoring pipeline

1. **Deterministic heuristic first** (`heuristic_clarity_score`): length
   penalties, vague tokens ("something", "whatever", "asap"), missing goal cue,
   missing specificity markers (paths, identifiers, quoted names, numbers). Free,
   fast, no I/O — safe to run on every intake path.
2. **LLM slow path behind the `Clarifier` protocol**:
   `async def assess(*, user_id: str, prompt: str) -> ClarityAssessment`.
   `default_clarifier()` is backed by `LLMService.complete` with
   `slot="background"` and `response_schema=ClarityAssessment` (expanded request,
   assumptions, open questions, score 0..1). Tests and the offline demo patch a
   fake behind this protocol.
3. **No-LLM fallback.** When no clarifier is available, a template draft is built
   from the heuristic score and the prompt itself, with generic open questions —
   the gate still functions, just with a thinner draft.

### Storage

`autonomy_ticket_clarifications`: score, expanded_request, assumptions,
open_questions, status, confirmed_text, timestamps. One active clarification per
ticket is enforced by a partial unique index (`UNIQUE (ticket_id) WHERE status =
'awaiting'`), so a double-draft is a constraint error, not a data bug.

### Resume semantics — the confirm is the single writer

- The ticket transition uses `update_ticket(..., expected_status=
  "awaiting_clarification")` as an optimistic fence. A confirm racing a cancel or
  a second confirm fails the fence and reports the winner's state instead of
  double-launching.
- The launch effect uses the idempotency key
  `autonomy:launch:{ticket_id}:clarified:{clarification_id}`. A retried confirm
  cannot enqueue a second launch, and a re-confirm after a fresh clarification
  uses a fresh key.
- The confirmed text becomes the goal: `scope` and `mandate.goal` are both set,
  so the worker reads the confirmed goal regardless of which field it looks at.

### Channel integration

- **Non-manual sources** get an outbox reply on the originating thread: the
  draft plus "is this what you mean?", enqueued with an idempotency key derived
  from the ticket id (one active clarification per ticket, so the key is unique
  per question), followed by the usual reply effect.
- **Manual tasks are UI-only** — no outbox row, the web panel is the surface.
- **Intake** treats a follow-up trigger on an `awaiting_clarification` ticket as
  the answer (`answer_via_channel`): the user replies in place on the channel,
  and the reply runs the same fence and launch-effect idempotency as the UI
  confirm.

### Config

| Env | Default | Meaning |
|---|---|---|
| `AUTONOMY_CLARIFY_ENABLED` | off | master switch for the gate |
| `AUTONOMY_CLARIFY_THRESHOLD` | 0.6 | clarity score below which a ticket is held |

### API

- `GET /api/v1/autonomy/tickets/{id}/clarification` — the active draft (score,
  expanded request, assumptions, open questions) for the panel.
- `POST /api/v1/autonomy/tickets/{id}/clarification/confirm` — body `{text}`;
  resumes the ticket. Returns the updated ticket, or 409 when there is no active
  clarification (already answered, cancelled, or never gated).

### UI

When a ticket is `awaiting_clarification`, the ticket detail shows a
Confirm / Edit-and-confirm panel: the expanded request pre-filled and editable,
the assumptions and open questions listed beneath it. Confirm POSTs the (possibly
edited) text; the user can also edit first so the channel bystanders (see
Tradeoffs) see a reviewed draft, not a raw model guess.

## Tradeoffs

- **Heuristic vs LLM-only scoring.** Heuristic is free and deterministic but
  shallow; LLM-only scores better but costs a model call per ticket and fails
  closed where no background credentials exist. The hybrid keeps the heuristic
  as the always-on fast path and the LLM as the authority when enabled, with the
  heuristic-built template as the fallback draft.
- **Default off vs always-on.** Default off means existing deployments keep their
  behavior until an operator opts in and tunes the threshold against their own
  traffic; always-on would gate vague-but-fine prompts and surprise users.
- **Draft on the ticket mandate vs a separate table.** Chose the table: the draft
  has its own lifecycle (awaiting -> confirmed), its own id (the launch
  idempotency key names it), and the partial unique index enforces one active
  draft per ticket. Keeping it out of `mandate` leaves the mandate as the
  worker's contract.
- **Channel replies are public.** On a public repo the clarification question is
  visible to bystanders. The outbox text is deliberately a question, and the
  expanded draft can be edited in the UI before the channel ever sees a follow-up.
- **No auto-expiry.** `awaiting_clarification` tickets sit until answered or
  cancelled. The watchdog deliberately ignores the state, so a ticket parked for
  a week does not accumulate stall attention — at the price of possibly-parked
  work, which nudging (Future work) would address.

## Compatibility

The gate is per-ticket and the launch path is unchanged. `ensure_launch_effects`
selects `WHERE status = 'queued'`, so a gated ticket is not launchable until
confirmed; the scheduler, its advisory-locked pass, and the effect runner are
untouched. Confirmations are per-ticket CAS transitions on rows in Postgres, so
parallel/multi-ticket operation — and multi-worker confirm handling — work exactly
as before. There is no cross-ticket gate state.

## Future work

- **Per-source thresholds.** GitHub comments, Feishu messages, and manual tasks
  have different vagueness baselines; one global threshold is a compromise.
- **Nudging.** Re-ping a clarification that has been unanswered for N hours; this
  needs a new sweep, since the watchdog intentionally ignores the state.
- **Confidence-calibrated thresholds.** Log score vs. confirm/edit outcome and fit
  the threshold to keep the false-positive rate under a target, instead of
  hand-tuning 0.6.
