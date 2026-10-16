# Project loops

## Test bloat consolidation loop

Consolidates duplicated test setup and redundant test cases in `tests/` one atomic pass at a time until no confirmed low-risk test bloat remains or the pass limit is reached.

- **Saved**: 2026-10-16
- **Source loops**:
  - [The housekeeper loop](https://signals.forwardfuture.com/loop-library/loops/housekeeper-loop/) (catalog modified date: 2026-07-07)
  - [The architecture-preserving code refactor loop](https://signals.forwardfuture.com/loop-library/loops/architecture-preserving-code-refactor-loop/) (catalog modified date: 2026-07-07)

Prompt:
> Inspect `tests/test_*.py` for duplicated mock setup, unparametrised variant functions, and cross-file assertion overlap. Each pass, consolidate one coherent cluster using shared fixtures or `@pytest.mark.parametrize` without deleting unique behavioural assertions or modifying production code. After each change, run `uvx codespell@latest -s`, `uvx ruff@latest check .`, `uvx pyright@latest`, and `uv run pytest`, keeping only passing reductions. Stop when no low-risk bloat remains or after 5 passes. Ask before deleting any unique test scenario.
