# Architecture & Code Walkthrough

This document provides a technical walkthrough of how the Gemini PR Review & Triage action is implemented, how it handles codebase context, and how the core Python scripts function.

---

## Architecture Overview

The action runs as a native GitHub Composite Action (`action.yml`) that boots a Python environment using `uv` for dependency management.

![Gemini Review & Triage Workflow](../assets/gemini_architecture.png)

---

## Code Base Organisation & Package Architecture

The code review action is organized into a modular Python package (`gemini_review/`) separating domain responsibilities, with `gemini_pr_review.py` serving as the top-level execution entrypoint and backward-compatible API facade:

* **`gemini_review/schemas.py`**: Pydantic schemas defining structured outputs (`InlineComment`, `ReviewResult`).
* **`gemini_review/config.py`**: Configuration loader for `gemini-review.toml` and timeout defaults.
* **`gemini_review/utils.py`**: Binary file exclusion, diff patch line parsing, token counting, repo file listing, and rule loaders.
* **`gemini_review/github.py`**: GitHub REST API client functions for PR files, comment threads, and review postings.
* **`gemini_review/skills.py`**: Agent skill metadata parser and instruction loader for workspace and built-in skills.
* **`gemini_review/developer_knowledge.py`**: MCP/RPC integration to search and fetch official Google developer documentation.
* **`gemini_review/prompts.py`**: Dynamic PR diff prompt construction, full/sparse codebase context generation, and prompt assembly.
* **`gemini_pr_review.py`**: Main CLI entrypoint script that re-exports all `gemini_review` package APIs and runs the primary review loop.

---

## 🔎 Pull Request Review Script (`gemini_pr_review.py`)

The PR review workflow is designed to retrieve PR details, collect local codebase context, build a structured prompt, and atomically submit line-specific reviews back to GitHub.

### 1. File Discovery & Filtering
* **PR Changes:** The script fetches the list of changed files and their diff patches from the GitHub API using `get_pr_files()`. Locally, it falls back to `git diff main...HEAD`.
* **Binary Exclusion:** Non-text files, binaries, lock files, and encrypted files are filtered out using `is_text_file()`.

### 2. Hybrid Codebase Context Engine
To provide Gemini with project-wide awareness, the script traverses the workspace to find all tracked files via `get_all_repo_files()`. It then sums the file sizes (excluding the changed PR files) to determine the context mode:

* **Full Context Mode (≤ 1.5 MB):**
  If the rest of the text files in the repository fit within the size limit, the script reads their full contents using `get_file_content()` and appends them to the prompt under the section `=== Repository Context (Full Codebase) ===`.
* **Sparse Context Mode (> 1.5 MB):**
  If the repository is large, the script generates a visual directory/file tree representation of the codebase using `generate_file_tree()`. It also reads the full contents of only the core documentation or manifest files matching `core_file_patterns` (like `*.md`, `package.json`, `go.mod`, etc.) using `is_core_file()`.

### 3. Gemini Context Caching Engine
To drastically reduce API costs and latency for large codebase contexts, `gemini_pr_review.py` incorporates native **Gemini Context Caching**:

* **Threshold Verification**: If the codebase context exceeds 100,000 characters (~32,768 tokens, Gemini's minimum caching requirement), context caching is automatically activated.
* **Active Cache Lookup (`client.caches.list()`)**: Before creating a new cache, the script queries active server-side caches matching the model-scoped repository display name (`repo-cache-{repo}-{model}`). It validates that the cache's model matches the requested model, skipping any caches created under a different model version to avoid `INVALID_ARGUMENT` errors.
* **Cache Provisioning (`client.caches.create()`)**: If no matching active cache handle for the current model is found, the script provisions a new `CachedContent` resource containing the codebase context, `system_instruction`, and pre-parsed `tools`.
* **Cost & Multi-Turn Optimisation**: Input tokens billed against the cached handle receive a **90% discount**. Furthermore, multi-turn tool interactions (such as Google Developer Knowledge MCP searches or skill lookups) reference the cached handle without re-billing the 250,000-token codebase context on subsequent turns.
* **Resilient Fallback**: If cache creation, lookup, or generation with cached content fails for any reason, the script seamlessly falls back to direct context generation without interrupting the CI review pipeline.

### 4. PR Comment & Discussion Thread History Engine
When enabled via `include_comment_history: 'true'` (default), `gemini_pr_review.py` fetches complete historical discussion context from the GitHub API:
* **Inline & Conversation Retrieval (`get_pr_comments()`)**: Fetches inline review comments (`pulls/{pr_number}/comments`) and general PR issue comments (`issues/{pr_number}/comments`) using `while True` pagination loops (`per_page=100`) to guarantee all historical comments are captured.
* **Thread Structuring (`format_pr_comment_history()`)**: Groups comments into root comments and nested developer replies per file and line number, presenting clear conversational timelines to Gemini.
* **Resolution Decision Matrix**: Instructs Gemini not to repeat suggestions that have been addressed in code, deferred, or explicitly justified by developers, while ensuring unresolved items without explanation or un-applied agreed fixes are re-flagged.

### 5. Structured Output Schemas
Gemini is forced to return structured JSON adhering to the Pydantic schemas:
* `InlineComment`:
  - `path`: File path.
  - `line`: Line number in the RIGHT (modified) side of the diff.
  - `side`: Diff side.
  - `severity`: Severity icon (`🔴`, `🟠`, `🟡`, `🟢`).
  - `comment_text`: Feedback string.
  - `code_suggestion`: Optional drop-in suggestion replacement.
* `ReviewResult`:
  - `summary`: High-level quality assessment.
  - `resolved_items`: List of previously raised review comments/threads resolved in the current PR iteration.
  - `general_feedback`: List of highlights or observations.
  - `comments`: List of `InlineComment` instances.

### 6. Resilient Review Submissions
Submitting reviews with line-specific comments via GitHub's API can be fragile (e.g. if the model specifies a line index that falls outside the diff range).
* **Atomic Run:** The script first attempts to post the summary, resolved items list (`### ✅ Resolved Items from Prior Reviews`), and all inline comments in a single transaction via `POST /repos/{owner}/{repo}/pulls/{number}/reviews`.
* **Resilient Fallback:** If the atomic post fails (e.g. returns HTTP 422), the script catches the failure, posts the review summary comment, and attempts to publish individual comments one-by-one. This ensures valid comments are still delivered while preventing a CI checkout block.

---

## 🏷️ Issue Triage Script (`gemini_issue_triage.py`)

The issue triage script automatically categorises and labels new issues to streamline management.

### 1. Label Triage Retrieval
* The script calls `get_available_labels()` to fetch all labels currently configured on the repository, handling pagination dynamically.

### 2. Prompting & Classification
* The system instruction (loaded from `gemini-triage.toml`) instructs the model to act as a triage assistant.
* The issue's title and body, along with the list of available labels, are passed to Gemini.
* Using structured output, Gemini returns a `TriageResult` containing:
  - `selected_labels`: The subset of repo labels that match the issue.
  - `reasoning`: The explanation for applying those labels.

### 3. API Label Application
* The script calls `apply_labels()` to add the selected labels to the issue on GitHub.

---

## 🛠️ Configuration Options

The action's behavior is configured via `gemini-review.toml`:

```toml
# Default configuration
description = "Reviews a pull request using Google Gemini"
prompt = "..."

# Codebase Context Configuration (Optional)
max_context_bytes = 1500000  # Size threshold in bytes to trigger Sparse Mode
core_file_patterns = [
    "*.md",
    "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "Gemfile",
    "composer.json", "*.csproj", "*.sln", "Dockerfile", "docker-compose.yml",
    "gemini-review.toml", "action.yml"
]

# Gemini Context Caching (Optional)
enable_context_caching = true  # Enable native Gemini Context Caching for large repos (default: true)
cache_ttl_seconds = 3600       # Cache TTL in seconds (default: 3600 / 1 hour)
```

