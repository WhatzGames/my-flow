---
name: project-testing
description: General project testing strategy selector. Use when Codex is asked to test, verify, validate, run checks, measure coverage, inspect test gaps, perform visual QA, or choose a testing approach for a software repository. Detect the project type first, then choose focused commands. For Freenet repositories owned by the user's own GitHub owners, use the global Freenet static build plus local-node wrapper visual testing approach and this skill's detector script when available.
---

# Project Testing

This companion plugin requires the target workspace configured by My Flow. Keep every shell `workdir`, generated artifact, and implementation change inside an immediate project worktree under `<target>/worktrees`; the plugin's `PreToolUse` guard enforces that boundary even when My Testing is invoked independently.

## Start Here

Choose the test strategy before running commands. If the bundled detector is available, run it from the repository root:

```sh
python3 /path/to/project-testing/scripts/select_test_strategy.py .
```

Use the real script path relative to this `SKILL.md`, for example `.../skills/project-testing/scripts/select_test_strategy.py`. If the script is unavailable, apply the same detection rules manually.

Treat the detector output as a starting point. Always inspect repository docs and scripts before running expensive, browser-based, network-dependent, or local-node commands.

## Strategy Selection

Prefer project-native commands in this order:

1. documented commands in `README.md`, `CONTRIBUTING.md`, or local docs;
2. repository wrapper scripts such as `scripts/build.sh`, `scripts/test.sh`, `justfile`, `Makefile`, or `cargo make`;
3. package-manager scripts such as `test`, `lint`, `typecheck`, `check`, or `build`;
4. framework defaults for the detected stack.

Keep testing proportional to the change, but include visual/browser verification for user-facing UI changes and coverage tooling for explicit coverage questions.

## Freenet Own-Repo Strategy

Use this path when all are true:

- the Git remote owner is listed in `MY_TESTING_OWNERS`, a comma-separated list of GitHub owners treated as the user's own repositories;
- the repository contains Freenet indicators such as `fdev`, `freenet`, Freenet contracts, `scripts/publish.sh`, Freenet web bridge code, or a Freenet web contract README; and
- the task asks for testing, validation, visual verification, coverage, UI QA, release confidence, or debug of Freenet-hosted rendering.

For Freenet own repos, use the global Freenet testing approach:

```sh
./scripts/build.sh
python3 -m http.server 4173 --directory dist --bind 127.0.0.1
```

Verify the built static site first at `http://127.0.0.1:4173/` with explicit desktop and mobile viewports. Use at least:

- desktop: `1440x1000`
- mobile: `390x844`

For visual checks, assert:

- wasm booted;
- screenshots were inspected;
- no visible element extends outside `window.innerWidth`;
- mobile `document.documentElement.clientWidth` equals page `scrollWidth`;
- console and network logs have no boot-blocking errors.

Then publish the same `dist/` output to the local Freenet node:

```sh
./scripts/publish.sh
```

Smoke-test the printed `http://127.0.0.1:7509/v1/contract/web/.../` URL separately:

- shell page loads;
- sandbox iframe exists;
- iframe navigates away from `about:blank` to the `?__sandbox=1` app URL;
- app body contains real UI text, not only the shell title;
- CSS, JavaScript, and wasm assets return `200` with expected MIME types;
- console and network logs have no boot-blocking errors;
- desktop and mobile screenshots still show no horizontal overflow.

If `$freenet-visual-dev` is available, use it for detailed browser caveats. If a repository-local visual script exists, prefer it over rewriting browser automation.

If the static site works but the Freenet URL is blank, debug shell, sandbox iframe, CSP, injected bridge, and asset loading before changing CSS.

## Common Defaults

Use these only when the repository has no stronger documented path:

- Rust: `cargo fmt --check`, `cargo test`; add clippy when already part of repository convention.
- Node: use the repo's package manager; run `test`, plus `lint`, `typecheck`, or `build` scripts when present.
- Python: `pytest`; add `ruff`, `mypy`, or coverage only when configured or requested.
- Go: `go test ./...`.
- .NET: `dotnet test`.
- Static/frontend UI: run locally and use browser screenshots plus DOM overflow checks for visual changes.

## Coverage

For coverage requests:

- detect existing coverage tooling before installing anything;
- prefer repository-native coverage scripts;
- report measured coverage separately from unmeasured gaps;
- distinguish "coverage tool absent" from "coverage is low";
- do not imply full coverage unless branch/function/line coverage was measured and reviewed.

## Reporting

Report:

- selected strategy;
- commands run;
- pass/fail result;
- visual artifacts inspected;
- coverage result or coverage-tooling gap;
- commands skipped and why;
- residual risk.
