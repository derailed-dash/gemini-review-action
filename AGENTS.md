# Project Rules: Gemini Review Action Development

This repository contains the codebase for the **Gemini Code Review & Issue Triage GitHub Action**. When modifying code or templates, adhere to the following project-specific standards:

## General Development

- **English UK**: Use English UK spelling in all code comments, user-facing output, and documentation (e.g. "customise", "normalise", "standardise").

## Python Guidelines

- **Python Version**: Target Python >=3.13.
- **Type Hinting**: Use modern PEP 585 built-in types (e.g. use lowercase `list`, `dict`, `tuple` instead of importing from `typing`).
- **Formatting and Lints**: Always format and check code using `ruff` and `codespell` from the root of the project:
  ```bash
  uvx codespell@latest -s
  uvx ruff@latest check --fix .
  ```

## Security & Path Integrity

- **Path Traversal Prevention**: Always use `os.path.commonpath` to verify containment when reading file paths or skill instructions supplied dynamically.
- **Platform Independence**: Standardise all path IDs and relative references using forward slashes (`/`) even on Windows runners (using `replace(os.sep, "/")`).

## Testing Standards

- **Coverage**: Any new helper functions, tool implementations, or configuration loaders must be accompanied by comprehensive tests in the `tests/` directory.
- **Mocking**: Properly mock all external network requests (such as GitHub API endpoints or the Google Developer Knowledge MCP JSON-RPC service).

## Local Execution & Testing Guidance

To execute `gemini_pr_review.py` or `gemini_issue_triage.py` locally for testing without running in GitHub Actions:

1. **Environment Variables**:
   Load `GEMINI_API_KEY` dynamically from the local `.env` file before invoking Python (do NOT hardcode API key values):
   ```bash
   export GEMINI_API_KEY=$(grep '^GEMINI_API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
   ```
   If a `.env` file is not available, ask the user for the key.

   
2. **Git Diff Context for Local PR Reviews**:
   - `gemini_pr_review.py` relies on `git diff main...HEAD`. Untracked local files are not included in `git diff`.
   - To review uncommitted or untracked local changes during dry-run testing, either stage the changes (`git add .`) or pass a mock diff in a short Python launcher script. The user may have forgotten to stage untracked changes.

3. **Execution Command**:
   - Always run using the virtual environment interpreter (`.venv/bin/python`) or `uv run python`.
   - Set `WaitMsBeforeAsync: 10000` (10 seconds) on `run_command` calls to allow the LLM response generation and Developer Knowledge API lookup to complete synchronously within the turn.

