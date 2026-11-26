# Project Rules: Gemini Review Action Development

This repository contains the codebase for the **Gemini Code Review & Issue Triage GitHub Action**. When modifying code or templates, adhere to the following project-specific standards:

## General Development

- **English UK**: Use English UK spelling in all code comments, user-facing output, and documentation (e.g. "customise", "normalise", "standardise").

## Action Input Parameters vs TOML Configuration

- **Input Parameters in Workflow YAML**: All operational configuration parameters (such as `skip_inline_suggestions`, `include_comment_history`, `language`, `persona`, `timeout`) MUST be configured via action input parameters in `action.yml`, workflow `.yml` files, and environment variables (`GEMINI_*`).
- **TOML Configuration Scope**: `gemini-review.toml` is strictly reserved for prompt text templates and custom system prompt overrides. Operational configuration parameters must NOT be placed in `gemini-review.toml`.

## Review Agent Instructions vs Repository Project Context

When designing or modifying review prompts and context assembly:
- **Reviewer Custom Instructions (System Prompt)**:
  - Custom instructions to guide the PR review agent (persona, tone, extra review rules) MUST strictly come from `.github/review-instruction-additions.md` (or root `review-instruction-additions.md` fallback).
  - These are loaded via `load_custom_instructions()` and appended to Gemini's system instructions.
  - General repository guideline files like `AGENTS.md` or `GEMINI.md` MUST NOT be loaded into `load_custom_instructions()` or the reviewer's system prompt.
- **Repository Project Context (User Prompt / Diff Context)**:
  - Files like `AGENTS.md`, `GEMINI.md`, and `CLAUDE.md` represent repository guidelines, coding standards, and project rules.
  - They MUST be attached as **project context** alongside the diff (in `build_codebase_context()`) so the model understands the project's rules when evaluating code changes.
  - In sparse context mode, they are prioritised at the very top of `candidate_core_files` so they are never squeezed out by byte limits.

## Python Guidelines

- **Python Version**: Target Python >=3.13.
- **Type Hinting**: Use modern PEP 585 built-in types (e.g. use lowercase `list`, `dict`, `tuple` instead of importing from `typing`).
- **Formatting, Lints, and Type Checking**: Always format, lint, and type-check code using `codespell`, `ruff`, and `pyright` from the root of the project:
  ```bash
  uvx codespell@latest -s
  uvx ruff@latest format .
  uvx ruff@latest check --fix .
  uvx ruff@latest format --check .
  uvx pyright@latest
  ```

## Security & Path Integrity

- **Path Traversal Prevention**: Always use `os.path.commonpath` to verify containment when reading file paths or skill instructions supplied dynamically.
- **Platform Independence**: Standardise all path IDs and relative references using forward slashes (`/`) even on Windows runners (using `replace(os.sep, "/")`).

## Testing Standards

- **Coverage**: Any new helper functions, tool implementations, or configuration loaders must be accompanied by comprehensive tests in the `tests/` directory.
- **Mocking**: Properly mock all external network requests (such as GitHub API endpoints or the Google Developer Knowledge MCP JSON-RPC service).

## Multimodal Context & Visual Diffing Standards

- **Single-Turn Cross-Attention**: Always pass baseline (`base_sha`) and head (`head_sha`) images together in a single Gemini request as sequential `types.Part` elements so the model evaluates visual changes via direct cross-attention without lossy intermediate text summaries.
- **Image Downscaling, SVG Rasterisation, Tiered `media_resolution` & MIME Allowlist**: All raster images exceeding `image_trigger_bytes` (default: 600 KB) MUST be downscaled via `optimize_image_bytes()` using Lanczos resampling towards `image_target_bytes` (default: 300 KB). SVGs MUST be rasterised to `image/png` in memory via `resvg_py` (`rasterize_svg_bytes()`) because the Gemini Vision API rejects `image/svg+xml`. PDFs MUST pass through uncompressed. Always verify candidate parts against `is_gemini_supported_mime()` (`GEMINI_SUPPORTED_PART_MIMES`) before constructing parts via `build_media_part()` (`medium` for Markdown context images, extra PDFs, and issue triage screenshots; `high` for active visual PR diffs, including `renamed` images; `'low'` MUST NEVER be used). Skip Markdown-linked and `extra_context_files` images already covered by `visual_diff_files`, and provide graceful fallbacks (first stripping `media_resolution` via `strip_media_resolution()` on `is_media_resolution_error()`, preserving active caches, then text-only) if multimodal caching or generation fails.
- **Path Traversal on Dynamic Markdown References**: When extracting image links from markdown (`extract_markdown_image_references()`), always enforce `os.path.commonpath` containment against the workspace root before reading image blobs.
- **Remote Triage Screenshot Security & Label Allowlist**: When fetching remote screenshots for issue triage (`fetch_remote_image()`), strictly require `https://`, reject loopback/private/metadata hosts, cap downloads at `max_triage_images` (default: 5), and only attach `Authorization: Bearer <GITHUB_TOKEN>` to trusted GitHub domains (`github.com`, `githubusercontent.com`). Always deduplicate and filter `triage.selected_labels` against the repository's `available_labels` allowlist before applying labels via the GitHub API.
- **Invariant Prefix Purity & Diff Budget Guardrails**: Keep static baseline context (`base_sha` repository files and baseline Markdown images read via `get_git_blob`) strictly ahead of the dynamic PR tail (`dynamic_pr_prompt`, `visual_diff_parts`, `dynamic_multimodal_parts`). Never strip `pr_filenames` from the `base_sha` baseline snapshot, always validate git refs against flag injection (`ensure_git_ref_available`), and never truncate diff patches when applying `max_diff_full_file_bytes` water-fill limits on `Full Current File Content`.
- **Multi-Turn `Content` Prefix Cache Boundary & `system_instruction` Purity (DO NOT REVERT)**:
  - **NEVER flatten `_assemble_direct_contents()` back into a single `UserContent` turn** when `codebase_context` or `codebase_multimodal_parts` are present. Controlled Vertex AI A/B benchmarks on `gemini-4-hydrogen` (`eu` and `global`) proved that collapsing static text, baseline images, and a changing PR diff into one `Content(role="user")` turn drops implicit cache hit rates to **`2.3%–14.7%`** (stopping right after `system_instruction + tools` whenever a commit or `/gemini-review` comment modifies the tail; even text-only single-turn prompts cap around `78.3%`). Isolating the static baseline in **Turn 0 (`role="user"`) -> Turn 1 (`role="model"`, `BASELINE_CONTEXT_ACK`) -> Turn 2 (`role="user"`, `dynamic_pr_prompt` + visual diff parts)** lifts implicit cache hits to **`95.2%`** across commits and PRs on both `global` and `eu`/`us`.
  - **NEVER re-introduce `!{echo $PULL_REQUEST_NUMBER}` into `starter-examples/gemini-review.toml`** or any dynamic per-PR identifier into `system_instruction`; PR numbers belong in Turn 2 (`_assemble_dynamic_prompt`) so `system_instruction` stays byte-identical across PRs on the same repo (note: `load_system_instruction()` still honours `!{echo $PULL_REQUEST_NUMBER}` if a user's custom TOML explicitly requests it).
  - **NEVER duplicate HEAD `AGENTS.md` / `GEMINI.md` into `system_instruction`** when already included in `codebase_context` / `baseline_included_files` / `text_files`; doing so wastes tokens and invalidates `system_instruction` caching on any PR editing workspace rules (only inject `load_workspace_rules()` as a fallback when uncovered, e.g. `diff_only` mode).
- **LLM Timeout Floor, Flex Inference & Streaming Fallback**: Always configure `types.HttpOptions(timeout=max(timeout, 300) * 1000)` (raised to a `600s` floor when `service_tier='flex'` on AI Studio) on the Gemini client and use `generate_content_with_stream_fallback()` so connection resets recover via streaming and AI Studio Flex `503`/`429` shedding retries and falls back cleanly to Standard tier (`service_tier` is AI Studio–only and must never be sent to Vertex AI).


## Local Execution & Testing Guidance

If you are asked to run a review locally, be sure to present the actual review output back to the user, as well as your relevant conclusions following the review execution.

To execute `gemini_pr_review.py` or `gemini_issue_triage.py` locally for testing without running in GitHub Actions:

1. **Environment Variables**:
   Load `GEMINI_API_KEY` dynamically from the local `.env` file before invoking Python (do NOT hardcode API key values):
   ```bash
   export GEMINI_API_KEY=$(grep -E '^(export[[:space:]]+)?GEMINI_API_KEY=' .env | cut -d= -f2- | tr -d '"' | tr -d "'")
   ```
   Alternatively, if `.env` already uses `export GEMINI_API_KEY=...`, you can run `source .env` or pass `--env-file .env` to `uv run`. If a `.env` file is not available, ask the user for the key.

   
2. **Git Diff Context for Local PR Reviews**:
   - `gemini_pr_review.py` relies on `git diff main...HEAD`. Untracked local files are not included in `git diff`.
   - To review uncommitted or untracked local changes during dry-run testing, either stage the changes (`git add .`) or pass a mock diff in a short Python launcher script. The user may have forgotten to stage untracked changes.

3. **Execution Command**:
   - Always run using the virtual environment interpreter (`.venv/bin/python`) or `uv run --locked --project . python`.
   - Set `WaitMsBeforeAsync: 10000` (10 seconds) on `run_command` calls to allow the LLM response generation and Developer Knowledge API lookup to complete synchronously within the turn.

4. **Context Cache Testing & Verification**:
   - `gemini_pr_review.py` automatically checks for active Gemini Context Caches matching `repo-cache-{repo}` via `client.caches.list()`.
   - If an active cache exists within the TTL window (default 1h), it reuses the cache handle directly to eliminate cache creation overhead and maximize cost savings.
   - When executing test scripts in automated or persistent terminals, avoid sending background signals (`SIGINT`) during active API generation calls.

ALWAYS count exactly how many times you executed a review, to allow the user to cross reference invocations against costs and token usage.

## Installation Skill Synchronisation (`install-gemini-code-review-action`)

- **Canonical Source & Local Workspace Copy**:
  - The `install-gemini-code-review-action` skill is canonically hosted in `derailed-dash/dazbo-agent-skills` (`../dazbo-agent-skills/skills/install-gemini-code-review-action/SKILL.md`), and mirrored in this repository at [.agents/skills/install-gemini-code-review-action/SKILL.md](.agents/skills/install-gemini-code-review-action/SKILL.md) so users can also install it directly via `npx skills add derailed-dash/gemini-review-action --skill install-gemini-code-review-action`.
- **Keeping the Skill in Sync**:
  - **When modifying action inputs, personas, or workflow templates here**: Update [.agents/skills/install-gemini-code-review-action/SKILL.md](.agents/skills/install-gemini-code-review-action/SKILL.md) in the same PR (enforced by `tests/test_skill_sync.py`), and copy the updated `SKILL.md` across to the canonical `../dazbo-agent-skills/skills/install-gemini-code-review-action/SKILL.md` repository (and `~/.gemini/config/skills/install-gemini-code-review-action/SKILL.md` if present).
  - **When pulling upstream updates from the canonical skills repo**: Copy from `../dazbo-agent-skills/skills/install-gemini-code-review-action/SKILL.md` (or run `npx skills update -g -y && ./scripts/sync_skills.sh`) into [.agents/skills/install-gemini-code-review-action/SKILL.md](.agents/skills/install-gemini-code-review-action/SKILL.md).

## Pushing to GitHub

Before pushing a change of anything non-trivial:
- Check if any documentation should be updated based on this PR and update accordingly. This includes (but is not limited to) README.md, AGENTS.md, ARCHITECTURE.md, and [.agents/skills/install-gemini-code-review-action/SKILL.md](.agents/skills/install-gemini-code-review-action/SKILL.md) (plus syncing back to `../dazbo-agent-skills`).
- Update the version number in `pyproject.toml` and run `uv lock` (because `action.yml` enforces `uv run --locked`, `uv.lock` MUST stay synchronised with `pyproject.toml`). Verify your proposed new version number before proceeding.

## Release Notes & Tagging Standards

When preparing GitHub releases:
- **Rolled-In Highlights with Collapsible Drop-Downs**: In release notes, roll in summaries of previous recent releases under `## 📜 Rolled-In Highlights from Previous Releases` using collapsible HTML `<details><summary><strong>vX.Y.Z: Title</strong></summary>...</details>` blocks (following the v1.6.1 style).
- **Floating Tag Maintenance**: Always create the specific versioned tag (e.g. `v1.6.3`) AND force-update the major floating tag (e.g. `git tag -fa v1 -m "Update v1 tag to v1.6.3" && git push origin v1 --force`) so standard action consumers tracking `@v1` automatically receive updates.


