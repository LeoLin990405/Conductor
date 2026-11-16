# AgOS router/outcome audit (absorbed reference)

Source: `LeoLin990405/agos` @ `ac22d67` (2026-11-04), MIT.

Absorbed from `plugins/dsh-agos-router` and `plugins/dsh-agos/lib`:

- `lib/outcomes.js` — normalizes route/council/plan/civ/fleet evidence into
  seven-field rows; does not merge task vocabularies it cannot prove.
- `lib/ledger.js` — atomic append-only route outcome ledger.
- `lib/shadow.js` — shadow selection record that never drives dispatch.
- `lib/sanitize.js`, `ids.js`, `ledger-lock.js`, `task-fingerprint.mjs` —
  dependencies for the above.
- `../dsh-agos/lib/secrets-gate.js` — the redaction classifier used by
  `sanitize.js`.
- `test/outcomes.test.mjs` — upstream no-network regression for the outcome
  derivation.

This is a **reference absorb**, not wired into Conductor's Python runtime.
The honesty/outcome coverage rules are the reason it is kept; Conductor's
autonomy router already owns route decisions and must not duplicate the ledger.

Run the standalone test:

```bash
node --test contrib/agos-router/test/outcomes.test.mjs
```

No credentials, `.env`, or fixtures containing candidate secrets were copied.
