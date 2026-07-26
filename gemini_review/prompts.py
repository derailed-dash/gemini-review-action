"""
Description: Prompt construction and system instruction loading module.
Constructs PR diff patches, codebase context (Full or Sparse mode),
and merges discussion thread history for Gemini model prompts.
"""

import os
import sys

from gemini_review.personas import get_persona_prompt, resolve_persona_name
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
            " and alignment with code changes. Review any prior PR comment history. DO NOT repeat suggestions that have"
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
        " replacement will duplicate surrounding code."
    )
    base_prompt = f"{base_prompt}\n\n{suggestion_instruction}"

    # Append reviewer persona prompt (e.g. 'straight', 'thorough') to base_prompt in either case
    persona_name = resolve_persona_name(config)
    print(f"Reviewer persona: '{persona_name}'", file=sys.stderr)
    persona_prompt = get_persona_prompt(persona_name)
    if persona_prompt:
        base_prompt = f"{base_prompt}\n\n{persona_prompt}"

    return base_prompt


def build_pr_diff_prompt(files: list) -> str:
    """Build the dynamic PR diff patch prompt for modified files."""
    fn_is_text_file = _get_pr_review_func("is_text_file", is_text_file)
    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    fn_format_diff_patch = _get_pr_review_func(
        "format_diff_patch_with_line_numbers", format_diff_patch_with_line_numbers
    )
    fn_format_file_content = _get_pr_review_func(
        "format_file_content_with_line_numbers", format_file_content_with_line_numbers
    )

    prompt_parts = []
    prompt_parts.append("Below are the files and changes included in this Pull Request:\n")

    for f in files:
        filename = f["filename"]
        status = f["status"]
        patch = f.get("patch", "")

        if not fn_is_text_file(filename) or not patch:
            continue

        full_content = fn_get_file_content(filename)

        prompt_parts.append(f"=== File: {filename} ===")
        prompt_parts.append(f"Status: {status}")
        prompt_parts.append("--- Diff (Patch) ---")
        prompt_parts.append(fn_format_diff_patch(patch))
        if full_content:
            prompt_parts.append("--- Full Current File Content ---")
            prompt_parts.append(fn_format_file_content(full_content))
        prompt_parts.append("=========================\n")

    return "\n".join(prompt_parts)


def build_codebase_context(files: list, config: dict) -> str:
    """Build the static repository codebase context for caching."""
    fn_get_all_repo_files = _get_pr_review_func("get_all_repo_files", get_all_repo_files)
    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    fn_is_core_file = _get_pr_review_func("is_core_file", is_core_file)
    fn_generate_file_tree = _get_pr_review_func("generate_file_tree", generate_file_tree)

    prompt_parts = []
    pr_filenames = {f["filename"] for f in files}

    max_context_bytes = config.get("max_context_bytes", 1500 * 1024)
    if "GEMINI_MAX_CONTEXT_BYTES" in os.environ:
        try:
            max_context_bytes = int(os.environ["GEMINI_MAX_CONTEXT_BYTES"])
        except ValueError:
            pass

    core_patterns = config.get(
        "core_file_patterns",
        [
            # Documentation
            "*.md",
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
                "Codebase context: running in Sparse Context Mode (attaching file tree and core"
                " manifests/documentation).",
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
            for f in other_files:
                if fn_is_core_file(f, core_patterns):
                    content = fn_get_file_content(f)
                    if content:
                        prompt_parts.append(f"--- File: {f} ---")
                        prompt_parts.append(content)
                        prompt_parts.append("-----------------\n")
                        core_files_included.append(f)
            if core_files_included:
                print(
                    f"Codebase context: attached {len(core_files_included)} core configuration/documentation files:"
                    f" {', '.join(core_files_included)}",
                    file=sys.stderr,
                )
            else:
                prompt_parts.append("(No additional key configuration or documentation files found.)\n")
                print("Codebase context: no core files matched or found.", file=sys.stderr)
            prompt_parts.append("==========================================\n")

    return "\n".join(prompt_parts)


def build_prompt(files: list, config: dict, comment_history: str = "") -> str:
    """Consolidate file patches, PR comment history, and file contents into a single review context."""
    fn_build_pr_diff_prompt = _get_pr_review_func("build_pr_diff_prompt", build_pr_diff_prompt)
    fn_build_codebase_context = _get_pr_review_func("build_codebase_context", build_codebase_context)

    pr_prompt = fn_build_pr_diff_prompt(files)
    parts = [pr_prompt]
    if comment_history:
        parts.append(comment_history)
    codebase_ctx = fn_build_codebase_context(files, config)
    if codebase_ctx:
        parts.append(codebase_ctx)
    return "\n\n".join(parts)
