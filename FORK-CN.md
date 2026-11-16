# Conductor attribution

Conductor is an independent Apache-2.0 project. It is not operated or endorsed
by the maintainers of the upstream project it grew out of.

Parts of the agent runtime, and of the workspace and tooling infrastructure
around it, are derived from LangAlpha (<https://github.com/ginlix-ai/LangAlpha>),
which is licensed under the Apache License 2.0. The upstream copyright notices
and the Apache-2.0 text are retained in [`LICENSE`](LICENSE), as that license
requires of the code that is still derived from it.

That inherited runtime is being retired in rounds 8 and 9. What this repository
keeps is Conductor's own Super-agent platform.

## Other attribution

- The Router/Worker control plane follows the DeepSeek Harness team's
  Super-agent design
  ([source post](https://x.com/DeepSeekHarness/status/2116857257245987236)).
  Conductor is an independent implementation and is not affiliated with
  DeepSeek.
- Per-module provenance, clean-room notes and attribution, where a module has
  them, are in [`docs/ports/`](docs/ports); internal fix logs are not published.
- Notices for third-party material Conductor ships (skills-manager, mfusepy,
  Source Han Sans, langchain-zai and the DeepSeek image-token port) are in
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
  There is no top-level `NOTICE` file; `contrib/mmteam/NOTICE` covers only that
  contributed directory.
- Code absorbed from archived repositories is listed in
  [`docs/ABSORBED.md`](docs/ABSORBED.md).
