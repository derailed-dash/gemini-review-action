"""
Description: Prompt templates and dynamic context selection engine.
Constructs review prompts, parses PR diffs, executes
dynamic context file selection using the primary Gemini model, and merges discussion thread history.
"""

import json
import os
import sys
from typing import Any

from google.genai import types

from gemini_review.budget import cap_file_content, max_file_bytes, report_capped
from gemini_review.personas import get_persona_prompt, resolve_persona_name
from gemini_review.schemas import DynamicContextSelection
from gemini_review.utils import (
    _get_pr_review_func,
    format_diff_patch_with_line_numbers,
    format_file_content_with_line_numbers,
    generate_file_tree,
    get_all_repo_files,
    get_file_content,
    is_core_file,
    is_text_file,
)


def select_dynamic_context_files(
    client: Any,
    model: str,
    files: list[dict],
    candidate_files: list[str],
    max_files: int = 8,
) -> tuple[list[str], str]:
    """Dynamically select the most relevant repository files for PR review context."""
    if not client or not candidate_files:
        return [], ""

    # Build concise diff/change summary for modified files
    modified_summary = []
    for f in files:
        fname = f.get("filename", "")
        status = f.get("status", "modified")
        patch = f.get("patch", "")
        # Include snippet of patch (first 40 lines per file to avoid token bloat during selection)
        patch_snippet = "\n".join(patch.splitlines()[:40]) if patch else "(no diff patch available)"
        modified_summary.append(f"File: {fname} (Status: {status})\nDiff Snippet:\n{patch_snippet}")

    diff_context = "\n\n".join(modified_summary)
    candidates_list_str = "\n".join(f"- {path}" for path in candidate_files)

    prompt = (
        "You are an expert principal software engineer analyzing a Pull Request to select the most valuable"
        " repository context for an in-depth code review.\n\n"
        f"### Modified Files in PR:\n{diff_context}\n\n"
        f"### Available Candidate Files in Repository:\n{candidates_list_str}\n\n"
        "### Context Selection Guidelines:\n"
        f"Select up to {max_files} of the most relevant candidate files to help the reviewer evaluate correctness,"
        " algorithmic efficiency, architectural alignment, and project idioms.\n"
        "Prioritize across these dimensions:\n"
        "1. **Direct Dependencies & Callers**: Modules directly imported by or importing the modified code.\n"
        "2. **Tests & Data Fixtures**: Corresponding unit tests, integration tests, or input fixtures.\n"
        "3. **Algorithmic & Domain Precedents**: Sibling modules solving similar domain problems or implementing"
        " related patterns (e.g. other search/traversal algorithms, handlers, controllers, or data models).\n"
        "4. **Shared Frameworks & Utilities**: Common base classes, coordinate/math utilities, schemas, or"
        " helpers.\n\n"
        f"From the candidate list above, select up to {max_files} files that are most relevant. Return ONLY valid"
        " paths from the candidate list.\n"
        "Provide a concise justification for your selection in 'reasoning'."
    )

    try:
        print(
            f"Dynamic context selection: evaluating {len(candidate_files)} candidate files with '{model}'...",
            file=sys.stderr,
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DynamicContextSelection,
                temperature=0.0,
            ),
        )

        raw_text = response.text or "{}"
        data = json.loads(raw_text)
        selection = DynamicContextSelection(**data)

        # Validate candidate membership
        valid_candidates = set(candidate_files)
        valid_selected = []
        for path in selection.selected_files:
            clean_path = path.strip().removeprefix("./")
            if clean_path in valid_candidates and clean_path not in valid_selected:
                valid_selected.append(clean_path)
            elif path in valid_candidates and path not in valid_selected:
                valid_selected.append(path)

        valid_selected = valid_selected[:max_files]
        return valid_selected, selection.reasoning
    except Exception as e:
        print(f"Warning: Dynamic context selection failed ({e}). Proceeding without dynamic files.", file=sys.stderr)
        return [], ""


def load_system_instruction(repository: str | None, pr_number: int, config: dict) -> str:
    """Load system instructions for Gemini code & technical reviews.

    Sets base_prompt to either the custom prompt template from gemini-review.toml
    (with variable substitutions) OR the default fallback prompt if no custom prompt
    is defined. In both cases, the configured reviewer persona prompt (e.g. 'straight',
    'thorough') is appended to base_prompt afterwards.
    """
    prompt = config.get("prompt", "")
    if not prompt:
        # Fallback base prompt if gemini-review.toml does not define a custom prompt key
        base_prompt = (
            "You are a world-class software engineering and technical review agent. Analyze changes across code,"
            " documentation, architecture, and configuration to output comprehensive, constructive feedback using"
            f" {os.environ.get('GEMINI_LANGUAGE', 'English (UK)')} spelling. Evaluate documentation updates for clarity"
            " and alignment with code changes. Do not make sweeping or universal claims (e.g. 'all dependencies/actions"
            " are pinned') in the summary or general feedback unless every single occurrence across the entire diff"
            " has been verified. Review any prior PR comment history. DO NOT repeat suggestions that have"
            " been addressed, deferred, or explicitly justified/disagreed with by the developer. DO restate unresolved"
            " suggestions if the code remains unchanged without an explanation or if the developer agreed with the fix"
            " but has not yet applied it."
        )
    else:
        # Custom prompt from gemini-review.toml: perform dynamic template variable substitutions
        prompt = prompt.replace("!{echo $REPOSITORY}", repository or "unknown")
        prompt = prompt.replace("!{echo $PULL_REQUEST_NUMBER}", str(pr_number))
        prompt = prompt.replace("!{echo $ADDITIONAL_CONTEXT}", "")

        language = os.environ.get("GEMINI_LANGUAGE", "English (UK)")
        base_prompt = prompt.replace("!{echo $LANGUAGE}", language)

    # Append inline suggestion guidance regarding line-range alignment
    suggestion_instruction = (
        "IMPORTANT FOR INLINE CODE SUGGESTIONS: GitHub inline suggestions replace EXACTLY the line range between"
        " start_line and line (inclusive). Whenever code_suggestion modifies or replaces multiple existing lines, you"
        " MUST provide start_line (start of replaced range) and line (end of replaced range). If start_line is omitted"
        " (single-line comment), code_suggestion MUST replace only that single line. Never include surrounding lines"
        " in code_suggestion unless start_line and line span all of those original lines, otherwise GitHub's inline"
        " replacement will duplicate surrounding code. NEVER include line numbers, line prefixes (e.g. '105 | ',"
        " '105 + | ', 'L105:'), or markdown code block fences in code_suggestion; code_suggestion MUST contain ONLY raw"
        " drop-in replacement code."
    )
    base_prompt = f"{base_prompt}\n\n{suggestion_instruction}"

    # Append reviewer persona prompt (e.g. 'straight', 'thorough') to base_prompt in either case
    persona_name = resolve_persona_name(config)
    print(f"Reviewer persona: '{persona_name}'", file=sys.stderr)
    persona_prompt = get_persona_prompt(persona_name)
    if persona_prompt:
        base_prompt = f"{base_prompt}\n\n{persona_prompt}"

    return base_prompt


def build_pr_diff_prompt(files: list, config: dict | None = None) -> str:
    """Build the dynamic PR diff patch prompt for modified files.

    The DIFF is always attached in full: it is what is under review, and it stays small
    even for enormous files. The FULL CURRENT CONTENT is capped per file, because one
    generated artifact (an OpenAPI spec, a snapshot, a bundled schema) can exceed the
    model's entire input window on its own and take the whole review down with it.
    """
    fn_is_text_file = _get_pr_review_func("is_text_file", is_text_file)
    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    fn_format_diff_patch = _get_pr_review_func(
        "format_diff_patch_with_line_numbers", format_diff_patch_with_line_numbers
    )
    fn_format_file_content = _get_pr_review_func(
        "format_file_content_with_line_numbers", format_file_content_with_line_numbers
    )

    limit = max_file_bytes(config)
    capped: list[str] = []

    prompt_parts = []
    prompt_parts.append("Below are the files and changes included in this Pull Request:\n")

    for f in files:
        filename = f["filename"]
        status = f["status"]
        patch = f.get("patch", "")

        if not fn_is_text_file(filename) or not patch:
            continue

        full_content = fn_get_file_content(filename)
        if full_content:
            full_content, was_capped = cap_file_content(full_content, filename, limit)
            if was_capped:
                capped.append(filename)

        prompt_parts.append(f"=== File: {filename} ===")
        prompt_parts.append(f"Status: {status}")
        prompt_parts.append("--- Diff (Patch) ---")
        prompt_parts.append(fn_format_diff_patch(patch))
        if full_content:
            prompt_parts.append("--- Full Current File Content ---")
            prompt_parts.append(fn_format_file_content(full_content))
        prompt_parts.append("=========================\n")

    report_capped(capped, limit)
    return "\n".join(prompt_parts)


def build_codebase_context(
    files: list,
    config: dict,
    client: Any = None,
    model: str | None = None,
) -> str:
    """Build the repository codebase context (Full or Sparse mode) for Gemini code review."""
    fn_get_all_repo_files = _get_pr_review_func("get_all_repo_files", get_all_repo_files)
    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    fn_is_core_file = _get_pr_review_func("is_core_file", is_core_file)
    fn_generate_file_tree = _get_pr_review_func("generate_file_tree", generate_file_tree)
    fn_select_dynamic_context_files = _get_pr_review_func("select_dynamic_context_files", select_dynamic_context_files)

    prompt_parts = []
    pr_filenames = {f["filename"] for f in files}

    max_context_bytes = config.get("max_context_bytes", 1500 * 1024)
    if "GEMINI_MAX_CONTEXT_BYTES" in os.environ:
        try:
            max_context_bytes = int(os.environ["GEMINI_MAX_CONTEXT_BYTES"])
        except ValueError:
            pass

    file_byte_limit = max_file_bytes(config)

    max_core_context_bytes = config.get("max_core_context_bytes", 500 * 1024)
    if "GEMINI_MAX_CORE_CONTEXT_BYTES" in os.environ:
        try:
            max_core_context_bytes = int(os.environ["GEMINI_MAX_CORE_CONTEXT_BYTES"])
        except ValueError:
            pass

    core_patterns = config.get(
        "core_file_patterns",
        [
            # Core documentation & project specification
            "README*",
            "CONTRIBUTING*",
            "ARCHITECTURE*",
            "DESIGN*",
            "SPEC*",
            "DEPLOYMENT*",
            "INSTALL*",
            "PRODUCT*",
            "PROD*",
            "SDD*",
            "TDD*",
            "TODO*",
            "CHANGELOG*",
            "SECURITY*",
            "GEMINI.md",
            "AGENTS.md",
            "CLAUDE.md",
            "docs/*.md",
            "docs/design/*.md",
            "docs/spec/*.md",
            "docs/architecture/*.md",
            # Shared templates, utilities & core modules
            "*template*",
            "*shared*",
            "*util*",
            "*utils*",
            "*common*",
            "*core*",
            "*helper*",
            "*helpers*",
            "*base*",
            # Python
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
            # JavaScript / TypeScript / Node
            "package.json",
            "tsconfig.json",
            # Go
            "go.mod",
            # Rust
            "Cargo.toml",
            # Java / Kotlin
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            # Ruby
            "Gemfile",
            "*.gemspec",
            # PHP
            "composer.json",
            # C# / .NET
            "*.csproj",
            "*.sln",
            # Swift / Objective-C
            "Package.swift",
            "Podfile",
            # Docker / Infrastructure
            "Dockerfile",
            "docker-compose.yml",
            # Configuration
            "gemini-review.toml",
            "action.yml",
        ],
    )

    repo_files = fn_get_all_repo_files()
    other_files = [f for f in repo_files if f not in pr_filenames]
    print(
        f"Codebase context: found {len(repo_files)} total tracked files, {len(other_files)} other files"
        " (excluding PR diff files).",
        file=sys.stderr,
    )

    if other_files:
        total_size = 0
        file_sizes = {}
        for f in other_files:
            try:
                size = os.path.getsize(f)
                file_sizes[f] = size
                total_size += size
            except Exception:
                continue

        print(
            f"Codebase context: total size of other text files is {total_size} bytes"
            f" (limit is {max_context_bytes} bytes).",
            file=sys.stderr,
        )

        if total_size <= max_context_bytes:
            print(
                "Codebase context: running in Full Context Mode (attaching all repository text files).", file=sys.stderr
            )
            prompt_parts.append("=== Repository Context (Full Codebase) ===")
            prompt_parts.append("Below are the contents of all other files in this repository for context:\n")
            for f in other_files:
                content = fn_get_file_content(f)
                if content:
                    prompt_parts.append(f"--- File: {f} ---")
                    prompt_parts.append(content)
                    prompt_parts.append("-----------------\n")
            prompt_parts.append("=========================================\n")
        else:
            print(
                "Codebase context: running in Sparse Context Mode (attaching file tree, core manifests,"
                " and dynamic context).",
                file=sys.stderr,
            )
            prompt_parts.append("=== Repository Context (Large Codebase) ===")
            prompt_parts.append(
                "Because this codebase is large, we have included the project file structure and key"
                " configuration/documentation files for context:\n"
            )

            full_tree_files = list(pr_filenames.union(set(other_files)))
            file_tree = fn_generate_file_tree(full_tree_files)
            prompt_parts.append("--- Repository File Structure ---")
            prompt_parts.append(file_tree)
            prompt_parts.append("---------------------------------\n")

            prompt_parts.append("--- Key Configuration and Documentation Files ---")
            core_files_included = []
            core_capped: list[str] = []
            core_bytes_used = 0
            for f in other_files:
                if fn_is_core_file(f, core_patterns):
                    try:
                        f_size = os.path.getsize(f)
                    except Exception:
                        f_size = 0
                    if core_bytes_used + f_size <= max_core_context_bytes:
                        content = fn_get_file_content(f)
                        if content:
                            content, was_capped = cap_file_content(content, f, file_byte_limit)
                            if was_capped:
                                core_capped.append(f)
                                f_size = min(f_size, file_byte_limit)
                            prompt_parts.append(f"--- File: {f} ---")
                            prompt_parts.append(content)
                            prompt_parts.append("-----------------\n")
                            core_files_included.append(f)
                            core_bytes_used += f_size
                    else:
                        print(
                            f"Codebase context: skipping core file '{f}' (exceeds max_core_context_bytes limit of"
                            f" {max_core_context_bytes} bytes).",
                            file=sys.stderr,
                        )
            report_capped(core_capped, file_byte_limit)
            if core_files_included:
                print(
                    f"Codebase context: attached {len(core_files_included)} core configuration/documentation files"
                    f" ({core_bytes_used} bytes): {', '.join(core_files_included)}",
                    file=sys.stderr,
                )
            else:
                prompt_parts.append("(No additional key configuration or documentation files found.)\n")
                print("Codebase context: no core files matched or found.", file=sys.stderr)

            # Dynamic context selection via model
            dynamic_candidates = [f for f in other_files if f not in core_files_included]
            if client and dynamic_candidates:
                effective_model = model or os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")
                selected_files, reasoning = fn_select_dynamic_context_files(
                    client=client,
                    model=effective_model,
                    files=files,
                    candidate_files=dynamic_candidates,
                )
                if selected_files:
                    print(
                        f"Dynamic context selection: selected {len(selected_files)} relevant file(s) using"
                        f" '{effective_model}': {', '.join(selected_files)}",
                        file=sys.stderr,
                    )

                    if reasoning:
                        print(f"Dynamic context selection reasoning: {reasoning}", file=sys.stderr)
                    prompt_parts.append("--- Relevant Codebase Context (Dynamically Selected) ---")
                    if reasoning:
                        prompt_parts.append(f"Selection Rationale: {reasoning}\n")
                    dynamic_capped: list[str] = []
                    for sf in selected_files:
                        content = fn_get_file_content(sf)
                        if content:
                            content, was_capped = cap_file_content(content, sf, file_byte_limit)
                            if was_capped:
                                dynamic_capped.append(sf)
                            prompt_parts.append(f"--- File: {sf} ---")
                            prompt_parts.append(content)
                            prompt_parts.append("-----------------\n")
                    report_capped(dynamic_capped, file_byte_limit)

            prompt_parts.append("==========================================\n")

    return "\n".join(prompt_parts)


def build_prompt(
    files: list,
    config: dict,
    comment_history: str = "",
    client: Any = None,
    model: str | None = None,
) -> str:
    """Consolidate file patches, PR comment history, and file contents into a single review context."""
    fn_build_pr_diff_prompt = _get_pr_review_func("build_pr_diff_prompt", build_pr_diff_prompt)
    fn_build_codebase_context = _get_pr_review_func("build_codebase_context", build_codebase_context)

    pr_prompt = fn_build_pr_diff_prompt(files, config)
    parts = [pr_prompt]
    if comment_history:
        parts.append(comment_history)
    codebase_ctx = fn_build_codebase_context(files, config, client=client, model=model)
    if codebase_ctx:
        parts.append(codebase_ctx)
    return "\n\n".join(parts)
