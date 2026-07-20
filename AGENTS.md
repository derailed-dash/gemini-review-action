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
