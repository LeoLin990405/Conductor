# Third-party notices

Conductor is licensed under the Apache License 2.0 (see [`LICENSE`](LICENSE)).
This file collects the notices for material from other projects that
Conductor ships. Code derived from LangAlpha keeps its notices in
[`LICENSE`](LICENSE), with provenance in [`FORK-CN.md`](FORK-CN.md). Code absorbed
from archived repositories keeps its own `LICENSE`/`NOTICE` under
[`contrib/`](contrib), listed in [`docs/ABSORBED.md`](docs/ABSORBED.md).

| Material | Where | License |
|---|---|---|
| skills-manager directory table | `src/skills_hub/adapters.py` | MIT |
| mfusepy 3.1.1 (vendored, unmodified) | `src/ptc_agent/core/sandbox/livefs_runtime/mfusepy.py` | ISC |
| Source Han Sans CN Regular (font) | `src/utils/fonts/SourceHanSansCN-Regular.otf` | SIL OFL 1.1 |
| langchain-zai 0.1.0 (vendored) | `src/llms/vendor/langchain_zai/` | no separate license file; added to LangAlpha in #297, shipped as received |
| DeepSeek image-token calculator (port) | `src/llms/deepseek_image_tokens.py` | upstream license not confirmed; deleted with R9b L21 |

## skills-manager

- Project: [xingkongliang/skills-manager](https://github.com/xingkongliang/skills-manager)
- Used in: [`src/skills_hub/adapters.py`](src/skills_hub/adapters.py), the per-agent
  skill-directory table. It is adapted from the built-in directory list in that
  project's `src-tauri/src/core/tool_adapters.rs` (`default_tool_adapters`).
  No other code is copied. See [`docs/ports/skills-hub.md`](docs/ports/skills-hub.md).
- License: MIT

```
MIT License

Copyright (c) 2026 Tianliang Zhang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## mfusepy

- Project: [mxmlnkn/mfusepy](https://github.com/mxmlnkn/mfusepy), version 3.1.1
- Used in: `src/ptc_agent/core/sandbox/livefs_runtime/mfusepy.py`, vendored
  unmodified. The file keeps its own header. This code belongs to the inherited
  sandbox runtime that R9b retires.
- License: ISC

```
Copyright (c) 2012 Terence Honles (maintainer)
Copyright (c) 2008 Giorgos Verigakis (author)

Permission to use, copy, modify, and distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## Source Han Sans

- Project: [adobe-fonts/source-han-sans](https://github.com/adobe-fonts/source-han-sans)
- Used in: `src/utils/fonts/SourceHanSansCN-Regular.otf`
- License: SIL Open Font License 1.1. "Copyright 2014-2025 Adobe, with Reserved
  Font Name 'Source'." The full license text sits next to the font in
  [`src/utils/fonts/LICENSE-SourceHanSans.txt`](src/utils/fonts/LICENSE-SourceHanSans.txt),
  as the OFL requires.

## langchain-zai

- Used in: `src/llms/vendor/langchain_zai/` (version 0.1.0)
- Provenance: this package arrived as part of the code received from
  [LangAlpha](https://github.com/ginlix-ai/LangAlpha) (Apache-2.0), where it sits
  at the same path. It was added there by LangAlpha's maintainers in
  [ginlix-ai/LangAlpha#297](https://github.com/ginlix-ai/LangAlpha/pull/297)
  (2026-07-02), which describes it as vendored "pending PyPI publication" and
  "re-vendors upstream `65c48cb`", without naming that upstream. No separate
  license file came with it; it is shipped as received, presumably under the
  LangAlpha repository's terms. A same-named public repository
  (`cyx1322/langchain-zai`, MIT) has a different package layout; whether it is
  related is not established. Unlike most inherited code, it is still used
  outside the PTC runtime: `src/llms/llm.py` (GLM dispatch) and
  `src/llms/operator_channel.py`.

## DeepSeek image-token calculator

- Used in: `src/llms/deepseek_image_tokens.py`
- Provenance: the module docstring says it was ported from the DeepSeek Harness
  `dsh-llm-deepseek` image-token module, which implements the provider's
  published image-token calculation. The license of that DeepSeek Harness module
  has **not been confirmed**. Its only user is the inherited PTC middleware
  `src/ptc_agent/agent/middleware/file_operations/deepseek_images.py`, so it is
  scheduled for deletion with that middleware in R9b line L21
  (the plan that retires the inherited PTC runtime).
  If it is ever needed outside PTC, it must be rewritten from DeepSeek's public
  documentation alone.
