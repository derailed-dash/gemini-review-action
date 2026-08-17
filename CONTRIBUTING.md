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
4. **Submit the PR:** Describe your changes clearly using the PR template, referencing any open issues it resolves.

---

## 🤖 AI Code Reviews & The Fork PR Workflow

This repository uses **Gemini Code Review Action** to provide automated, constructive feedback on Pull Requests.

### Internal vs Fork Pull Requests

* **Internal Branch PRs (within the repository):**
  The Gemini Code Review Action runs automatically whenever a PR is opened or updated.
* **External Fork PRs (from community contributors):**
  For security reasons, GitHub Actions does **not** pass repository secrets (such as `GEMINI_API_KEY`) to workflows triggered directly by external forks. To prevent failing check runs, automatic execution on fork `pull_request` events is skipped.

### Triggering Reviews via `/gemini-review`

A repository maintainer will trigger the AI review on external fork PRs by posting a comment:

```text
/gemini-review
```

This triggers the review workflow with full access to repository secrets, checks out the PR code, and posts inline review comments and suggestions directly to your PR.

> [!TIP]
> Contributors are also welcome to ask a maintainer to run `/gemini-review` if they would like fresh AI feedback after pushing updates.

### Working with Gemini Review Feedback

1. **Inline Suggestions:** Actionable recommendations are formatted as GitHub suggestion blocks. You can apply them directly from the GitHub UI using the **Apply suggestion** button.
2. **Discussion & Iteration:** If you disagree with a suggestion or choose an alternative architectural approach, simply reply to the review comment thread explaining your rationale.
3. **Tracking Resolutions:** When `/gemini-review` is re-run on subsequent pushes, Gemini tracks previous comment history. It acknowledges resolved items under `### ✅ Resolved Items from Prior Reviews` and respects developer explanations for deferred or disagreed points.

---

## 🌟 Exemplar Pull Request: A Real-World Example

If you are looking for a great example of a well-crafted pull request in this repository, check out:

👉 **[PR #33: Feat: Add pricing support for Gemini 3.7 Flash cache reads](https://github.com/derailed-dash/gemini-review-action/pull/33)**

### Why this is a great example:
* **Focused & Well-Scoped:** Adds a specific, high-value capability (`DEFAULT_CACHE_READ_MULTIPLIER` for Gemini 3.7 Flash) without unnecessary scope creep.
* **Comprehensive Test Coverage:** Accompanied by unit tests in `tests/test_pr_review.py` verifying both default pricing multipliers and model-specific overrides.
* **Conventional Commits:** Follows standard commit conventions (`feat(model): ...`).
* **Interactive AI Review Lifecycle:** Demonstrates the Gemini Code Review Action in practice—Gemini provided an inline suggestion on cost calculation logic, which the author applied directly via GitHub suggestions before merging.

