# Contributing to Gemini PR Review & Triage Action

First off, thank you for considering contributing to this action! It's contributions like yours that make open-source software a wonderful place to learn, inspire, and create.

To ensure a smooth collaboration, please follow the guidelines below.

---

## 🛠️ Local Development Setup

This project uses [uv](https://github.com/astral-sh/uv) for fast, deterministic Python package and environment management.

1. **Fork and Clone the Repository:**
   ```bash
   git clone https://github.com/your-username/gemini-review-action.git
   cd gemini-review-action
   ```

2. **Sync Dependencies and Setup Environment:**
   Run `uv sync` to automatically create a virtual environment (`.venv`) and install all runtime and development dependencies:
   ```bash
   uv sync
   ```
   *Tip: Configure your code editor/IDE to point to the local `.venv/` folder to resolve imports and enable syntax checkers.*

---

## 🔍 Code Standards & Validation

We enforce strict formatting, spelling, and test coverage. Before submitting any changes, run the following verification steps from the project root:

1. **Verify Spelling:**
   ```bash
   uvx codespell@latest -s
   ```

2. **Lint and Auto-Fix Style Issues:**
   We use `ruff` to perform static code checks and verify formatting:
   ```bash
   uvx ruff@latest check --fix .
   ```

3. **Run Unit Tests:**
   Ensure all tests compile and pass successfully:
   ```bash
   uv run pytest
   ```

---

## 📝 Commit Message Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification for all repository commit logs. This assists in automated changelog generation.

Format:
`type(scope): description`

**Allowed Types:**
* `feat`: A new feature (e.g. `feat(triage): support custom labels`)
* `fix`: A bug fix (e.g. `fix(review): handle empty diff edge-case`)
* `docs`: Documentation updates only (e.g. `docs: update setup examples`)
* `style`: Code style changes (white-space, formatting, etc.)
* `refactor`: Restructuring code without changing behavior
* `test`: Adding or correcting tests
* `chore`: Auxiliary tool changes, build settings, or dependencies

---

## 🚀 Submitting Pull Requests

1. **Create a Feature Branch:** Branch off from `main` using descriptive names (e.g. `feat/my-new-feature` or `fix/issue-description`).
2. **Write Clean Code & Tests:** Make sure new features or bug fixes have corresponding unit tests in the `tests/` directory.
3. **Verify Locally:** Ensure `pytest`, `ruff`, and `codespell` all pass cleanly.
4. **Submit the PR:** Describe your changes clearly in the pull request description, referencing any open issues it resolves.
