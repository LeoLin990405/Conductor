# Change-impact map

`conductor impact` is a local, read-only blast-radius report for Python and TypeScript in one checkout. It does not call GitHub, a model provider, or the Conductor API.

The index is a sqlite file at `<repo>/.conductor/impact/index.sqlite`. The
default path is guarded: if `.conductor/impact/index.sqlite` (or a parent)
resolves outside the checkout through a symlink, the command refuses to open it
instead of dropping tables in another database. An explicit `--index` path is
the caller's choice and is not guarded. Each source file is keyed by mtime,
size, and a sha256 of its bytes. A second run parses a file again only when the mtime or size changed and the hash changed. Symbols, call sites, and imports for `.py`, `.ts`, and `.tsx` are stored there. The parser is tree-sitter with the MIT Python and TypeScript grammars (`tree-sitter`, `tree-sitter-python`, `tree-sitter-typescript`). Nothing in this tool comes from GitNexus.

## Commands

From the repository root:

```text
conductor impact pkg/lib.py
conductor impact pkg/lib.py::helper
conductor impact helper --json
conductor impact --review HEAD~1..HEAD
conductor impact --review base...head --json
```

A file argument (or a directory under the repo) seeds every function, method, and class in that file. A symbol argument is either a short name or a qualified name, `path::Name`. A short name that matches more than one symbol is not guessed. The report lists those candidates under `ambiguous` and leaves the affected sets empty until you pass a qualified name or a file.

`--json` prints the same object the text view summarizes inside the CLI's v1 envelope (under `data`). `--root` and `--index` select the checkout and the sqlite file. `--depth` limits how many caller hops are followed (default 5, maximum 20). `--review` does not take file or symbol arguments. It runs `git diff` locally. It does not fetch.

## What "affected" means

The report walks callers, not callees. Changing `helper` lists functions that call `helper`, then functions that call those, and the files that contain them. Tests are files under a `tests/` or `__tests__/` directory, `test_*.py`, `*_test.py`, and `*.test.ts(x)` / `*.spec.ts(x)`, plus functions whose name starts with `test_`. A test counts when it calls the symbol, imports it, or its filename matches the module stem (`test_lib.py` for `lib.py`).

`confidence` is a static certainty score from 0 to 1, not a probability that a test will fail. The overall score is the weakest direct caller. If any relevant call is a name collision, the overall score is capped at 0.5. A score of 1.0 with only the seed listed means no static caller was found. It does not mean the change is safe.

| Match | Confidence |
|---|---|
| Call by name to the only function or class visible in that scope | 0.95 |
| `self` / `cls` / `this` / `super` method on the enclosing class | 0.93 |
| Imported name, `module.function`, or `<Component />` with one resolution | 0.90 |
| `Class.method` or `new Class().method` when the class resolves | 0.88 |
| `from module import *` with one module-level match | 0.60 |
| One module-level function or class with that name in the whole index | 0.55 |
| File imports the module but no call resolved | 0.45 |
| Test filename matches the module stem | 0.40 |
| Each extra caller hop | × 0.85 |

Qualified names look like `pkg/lib.py::Helper.run`. JSX tags that start with an uppercase letter are treated as component calls. Lowercase tags are HTML and are ignored.

A definition hides an import of the same name in that scope. After `from a import Worker` and a later `class Worker`, `Worker.run()` resolves to the local class. A function-local import still hides the outer definition. A parameter does too: `from helpers import parse` then `def run(parse): return parse()` does not call `helpers.parse`, and neither does a function nested inside `run`. A function-local import still hides the parameter. TypeScript parameter patterns bind the names they introduce, so `function f({parse})` and `([parse]) => parse()` hide an imported `parse`. `{parse: local}` binds `local`. A default on the pattern still calls the default expression (`{parse = fallback()}`). Python defaults stay in the enclosing scope, so the `parse()` in `def run(parse=parse())` is the import, while `parse()` in the body is the parameter. `import package.submodule` binds the name `package`, so `package.func()` is the package attribute and `package.submodule.func()` is the submodule. `import package.submodule as name` binds `name` to the submodule.

A Python import in a class body binds that name only for statements that run in the class body: assignments, decorators, default values, and the iterable of a comprehension. It does not replace a module import of the same name. Methods, nested functions, lambdas, and the comprehension body do not see it. A nested class body does not see the outer class body either.

TypeScript default parameters run when the function is called. `const run = (x = helper()) => x` makes `run`, and functions that call `run`, callers of `helper`. Python defaults run when the `def` statement executes, in the enclosing scope, so they are not calls from the function being defined. An anonymous arrow has no symbol; its default stays on the enclosing function.

## Review

`conductor impact --review <base>..<head>` (or `base...head`) diffs the two revisions and maps hunks to the function, method, or class that owns each changed line. A line inside a nested function belongs to that function, not to its parent. The parent is still changed when a line in its own body changes, including when a nested function changes in the same diff. A pure deletion inside a function still marks that function. `base...head` reads the old file from the merge base, the same revision `git diff` uses for a three-dot range. For each changed symbol it lists direct callers that were not edited. A caller changed only by deleted lines counts as edited.

A symbol is flagged when at least one of those callers was not edited and no affected test file is in the diff. Updating the test file, or editing every direct caller, clears the flag. Deleting a function also flags an untouched `lib.helper()` or `pkg.lib.helper()` caller, an alias such as `from lib import helper as h` followed by `h()`, and a TypeScript default import (`import h from './lib'; h()`) of a removed `export default`. Deleting the whole module still flags a surviving `from pkg.lib import helper` caller. A parameter that reuses the deleted name is not a caller: `def main(helper): return helper()` still runs after `helper` is removed. Module-level statements execute top to bottom: a module-level call keeps the binding that existed at its position, so `from lib import helper; use(); from other import helper` still reports the `lib` call after `helper` is removed. A call inside a function is not filtered by its textual position: the function may run after later module statements. A resolved call does not hide a different call on the same line, so `wrapper(helper())` still reports `helper` after `helper` is removed. Callers found only through a name collision stay in `ambiguous` and are reported at confidence 0.25 when nothing unique was edited. The command exits 0 even when it flags something. A bad revision or a git error exits 1.

`git diff` is run with `--no-ext-diff` and `--no-textconv` for both `base..head` and `base...head`. A textconv filter cannot hide the changed lines or run a converter.

Git quotes non-ASCII paths with C-style octal escapes. `café.py` appears as `"a/caf\303\251.py"`. The review decodes that back to the working-tree path before it matches symbols.

The diff is read from git. Callers are resolved from the working tree index, so the checkout should match `<head>`.

Text output is sanitized at the print boundary: repository paths and symbol names cannot carry terminal control sequences into the operator's terminal, and JSON output escapes C0/C1 controls.

## Static blind spots

The index only records calls and imports that appear as syntax. These patterns do not create an edge, so a real caller can be missing and the confidence can stay high:

- **Reflection and `getattr`.** `getattr(obj, "helper")()` and `getattr(obj, name)()` are calls to `getattr`. The string or variable is not a callee.
- **Dynamic dispatch.** `obj[name]()`, `dispatch[key]()`, and computed members such as `obj[method]()` are not attached to a function. The same is true of `eval`, `exec`, and `importlib.import_module(name)`.
- **Dependency injection.** A class or function passed into a container (`provide(Service)`, a constructor parameter, a pytest fixture argument) is an argument, not a call. The tool does not know that the container will call it later.
- **String routes and registries.** `ROUTES = {"/users": users}`, `add_route("/users", users)`, and decorator factories that take the path as a string do not record `users` as a callee. A route table, signal name, or entry-point string is invisible.
- **Callbacks and higher-order use.** `router.get("/users")(users)` records a call to `router.get`, not to `users`, when `users` is only passed in. The same gap applies to `map(fn, items)` only in that `fn` is not treated as called; the call to `map` is recorded.
- **TypeScript path aliases and dynamic import.** Specifiers such as `@/components/widget`, bare package names, and `import(path)` are not resolved. Only relative specifiers (`./`, `../`) are, including a `.js` suffix that maps to `.ts` or `.tsx`.
- **Re-exports and function-local imports.** `export { foo } from "./foo"` is not followed. An import inside a function is visible to that function and to functions nested in it, and it is not limited to branches inside the function.
- **Runtime patching.** Monkeypatching, `setattr`, assignment of a function onto a class after the class body, and dependency-injection frameworks that look up a type by name leave no call edge.

Ambiguous short names are listed instead of being folded into one caller. Two modules that both define `helper`, with a third file calling `helper()` and importing neither, produce an `ambiguous` entry and no affected function.

## Layout

The implementation lives in `src/conductor_cli/impact/`:

- `extract.py` reads symbols, calls, and imports.
- `index.py` maintains the sqlite file.
- `analyze.py` resolves calls and builds the report.
- `review.py` maps a local git diff onto that report.
- `cli.py` is the `conductor impact` handler.

Hidden directories, `.git`, `.venv`, `node_modules`, and other build or cache directories are not indexed. Files larger than 1.5 MB are skipped. If a file that was already indexed grows past that limit, its stored symbols, calls, and imports are removed.
