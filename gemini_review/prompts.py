"""Prompt templates, custom instructions, and visual diff assembly.

This module provides functions for loading custom system instructions, persona overlays,
formatting unified diff patch prompts, assembling side-by-side visual image diffs,
and re-exporting the codebase context engine for backward compatibility.
"""

import os
import sys
from typing import Any

from google.genai import types

from gemini_review.budget import (
    cap_file_content,
    cap_patch_content,
    compute_waterfill_cap,
    max_diff_full_file_bytes,
    max_file_bytes,
    report_capped,
    report_capped_patches,
)
from gemini_review.config import (
    get_image_target_bytes,
    get_image_trigger_bytes,
    get_max_multimodal_images,
)
from gemini_review.context import (
    build_codebase_context,
    select_dynamic_context_files,
)
from gemini_review.diff import (
    format_diff_patch_with_line_numbers,
    format_file_content_with_line_numbers,
)
from gemini_review.github import get_file_blob
from gemini_review.multimodal import (
    get_mime_type,
    is_gemini_supported_mime,
    is_supported_image,
    optimize_image_bytes,
)
from gemini_review.personas import get_persona_prompt, resolve_persona_name
from gemini_review.repo import (
    _normalise_relative_posix,
    generate_file_tree,
    get_all_repo_files,
    get_file_content,
    get_git_blob,
    is_core_file,
    is_text_file,
    rank_and_bound_candidates,
)
from gemini_review.utils import _get_pr_review_func

__all__ = [
    # Re-exported context engine & repo helpers
    "build_codebase_context",
    "select_dynamic_context_files",
    "get_all_repo_files",
    "get_file_content",
    "is_core_file",
    "generate_file_tree",
    "is_text_file",
    "rank_and_bound_candidates",
    # Prompts & instructions
    "DEFAULT_CUSTOM_INSTRUCTIONS_PATH",
    "FALLBACK_CUSTOM_INSTRUCTIONS_PATH",
    "load_custom_instructions",
    "load_system_instruction",
    "build_pr_diff_prompt",
    "build_prompt",
    "build_visual_diff_parts",
]

DEFAULT_CUSTOM_INSTRUCTIONS_PATH = ".github/review-instruction-additions.md"
FALLBACK_CUSTOM_INSTRUCTIONS_PATH = "review-instruction-additions.md"


def _safe_read_instruction_file(candidate: str, workspace_root: str) -> str | None:
    """Safely read instruction file ensuring no path traversal outside workspace root."""
    norm_candidate = candidate.replace("/", os.sep).replace("\\", os.sep)
    full_path = os.path.realpath(norm_candidate)
    try:
        if os.path.commonpath([workspace_root, full_path]) != workspace_root:
            print(
                f"Warning: Access denied for custom instructions path '{candidate}' (path traversal blocked).",
                file=sys.stderr,
            )
            return None
    except Exception:
        return None

    if os.path.exists(full_path) and os.path.isfile(full_path):
        try:
            print(f"Loading custom review instructions from {candidate}...", file=sys.stderr)
            with open(full_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            print(f"Warning: Failed to read custom instructions from {candidate}: {e}", file=sys.stderr)
            return None
    return None


def load_custom_instructions(custom_input: str | None = None) -> str:
    """Load custom review instructions and guardrails for the PR review agent.

    Supports reading from custom_input argument, GEMINI_CUSTOM_INSTRUCTIONS environment
    variable, or convention-based file locations:
    .github/review-instruction-additions.md (preferred) or review-instruction-additions.md at repository root.
    Safely prevents path traversal. If the default file does not exist, returns an empty string.

    Note: General repository rule files (e.g. AGENTS.md, GEMINI.md) provide project context for diffs
    and are attached separately in codebase context, not as reviewer instructions.
    """
    raw_val = custom_input if custom_input is not None else os.environ.get("GEMINI_CUSTOM_INSTRUCTIONS")
    if raw_val is None:
        raw_val = DEFAULT_CUSTOM_INSTRUCTIONS_PATH

    raw_val = raw_val.strip()
    if not raw_val:
        return ""

    is_default = raw_val == DEFAULT_CUSTOM_INSTRUCTIONS_PATH
    workspace_root = os.path.realpath(".")

    if is_default:
        for candidate in [DEFAULT_CUSTOM_INSTRUCTIONS_PATH, FALLBACK_CUSTOM_INSTRUCTIONS_PATH]:
            content = _safe_read_instruction_file(candidate, workspace_root)
            if content:
                return content
        return ""

    # Explicit custom input provided
    content = _safe_read_instruction_file(raw_val, workspace_root)
    if content:
        return content

    # Multi-line string is definitely inline text instructions
    if "\n" in raw_val:
        return raw_val

    # If it looks like a path but wasn't found, warn and return empty
    if (
        raw_val.startswith(("./", "../", ".github/"))
        or raw_val.endswith((".md", ".txt", ".markdown"))
        or (os.sep in raw_val and " " not in raw_val and not raw_val.startswith("-"))
    ):
        print(f"Warning: Custom instructions file '{raw_val}' not found.", file=sys.stderr)
        return ""

    # Otherwise, treat it as single-line inline instruction text
    return raw_val


def load_system_instruction(
    repository: str | None,
    pr_number: int,
    config: dict,
    custom_instructions: str = "",
) -> str:
    """Load system instructions for Gemini code & technical reviews.

    Sets base_prompt to either the custom prompt template from gemini-review.toml
    (with variable substitutions) OR the default fallback prompt if no custom prompt
    is defined. In both cases, the configured reviewer persona prompt (e.g. 'straight',
    'thorough') is appended to base_prompt afterwards.
    """
    instructions_text = custom_instructions.strip() if custom_instructions else ""
    appended_instructions = False

    prompt = config.get("prompt", "")
    if not prompt:
        # Fallback base prompt if gemini-review.toml does not define a custom prompt key
        base_prompt = (
            "You are a world-class software engineering and technical review agent. Analyse changes across code,"
            " documentation, architecture, and configuration to output comprehensive, constructive feedback using"
            f" {os.environ.get('GEMINI_LANGUAGE', 'English (UK)')} spelling. Evaluate documentation updates for clarity"
            " and alignment with code changes. When visual diffs (base and head images), architectural diagrams, or"
            " specification documents are attached, visually inspect them for regressions, clarity, layout consistency,"
            " and alignment with the implementation. Do not make sweeping or universal claims"
            " (e.g. 'all dependencies/actions are pinned') in the summary or general feedback unless every single"
            " occurrence across the entire diff has been verified. Review any prior PR comment history. "
            "DO NOT repeat suggestions that have"
            " been addressed, deferred, or explicitly justified/disagreed with by the developer. DO restate unresolved"
            " suggestions if the code remains unchanged without an explanation or if the developer agreed with the fix"
            " but has not yet applied it."
        )
    else:
        # Custom prompt from gemini-review.toml: perform dynamic template variable substitutions
        prompt = prompt.replace("!{echo $REPOSITORY}", repository or "unknown")
        prompt = prompt.replace("!{echo $PULL_REQUEST_NUMBER}", str(pr_number))

        if "!{echo $ADDITIONAL_CONTEXT}" in prompt:
            prompt = prompt.replace("!{echo $ADDITIONAL_CONTEXT}", instructions_text)
            appended_instructions = bool(instructions_text)
        else:
            prompt = prompt.replace("!{echo $ADDITIONAL_CONTEXT}", "")

        language = os.environ.get("GEMINI_LANGUAGE", "English (UK)")
        base_prompt = prompt.replace("!{echo $LANGUAGE}", language)

    # If custom instructions are provided and haven't already been substituted into !{echo $ADDITIONAL_CONTEXT}
    if instructions_text and not appended_instructions:
        base_prompt = f"{base_prompt}\n\n## Additional Review Instructions & Guardrails:\n{instructions_text}"

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


def build_pr_diff_prompt(
    files: list,
    config: dict | None = None,
    *,
    include_full_content: bool = True,
    max_patch_bytes: int | None = None,
    excluded_files: list[str] | None = None,
    omitted_non_text_files: list[str] | None = None,
    baseline_included_files: set[str] | None = None,
) -> str:
    """Build the dynamic PR diff patch prompt for modified files.

    Newly added and removed files omit the duplicate ``Full Current File Content`` block because
    their entire content already appears inside the unified diff patch (or no longer exists). Files
    already present in ``baseline_included_files`` (invariant ``base_sha`` context prefix) likewise
    omit duplicate full-file copies. Remaining full-file blocks respect both per-file limits and the
    aggregate ``max_diff_full_file_bytes`` water-fill budget, while diffs themselves remain intact
    unless ``max_patch_bytes`` is triggered by an out-of-window budget fallback.
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
    aggregate_limit = max_diff_full_file_bytes(config)
    aggregate_explicit = "GEMINI_MAX_DIFF_FULL_FILE_BYTES" in os.environ or (
        config is not None and "max_diff_full_file_bytes" in config
    )
    baseline_set = {_normalise_relative_posix(p) for p in baseline_included_files} if baseline_included_files else set()
    omitted: list[str] = list(omitted_non_text_files) if omitted_non_text_files else []
    omitted_seen = set(omitted)
    capped: list[str] = []
    capped_patches: list[str] = []

    prompt_parts = []
    prompt_parts.append("Below are the files and changes included in this Pull Request:\n")
    if excluded_files:
        sample = ", ".join(excluded_files[:10])
        more = f", and {len(excluded_files) - 10} more" if len(excluded_files) > 10 else ""
        prompt_parts.append(
            f"Note: {len(excluded_files)} changed file(s) were intentionally excluded from this diff by "
            f"diff_exclude_patterns ({sample}{more}). Do not assume they are missing from the repository.\n"
        )

    # Pre-load eligible raw contents so both per-file and aggregate water-fill limits resolve in
    # a single truncation pass (keeping original byte counts accurate in markers and avoiding
    # duplicate stderr warnings).
    prepared: list[tuple[dict, str, str | None, bool]] = []
    effective_sizes: list[int] = []
    full_indices: list[int] = []
    disabled_by_zero = False

    for f in files:
        filename = f["filename"]
        status = f.get("status", "modified")
        patch = f.get("patch", "")

        if not fn_is_text_file(filename):
            if not is_supported_image(filename):
                entry = f"{filename} ({status})"
                if entry not in omitted_seen:
                    omitted_seen.add(entry)
                    omitted.append(entry)
            continue
        if not patch:
            continue

        formatted_patch = fn_format_diff_patch(patch)
        if max_patch_bytes is not None and max_patch_bytes > 0:
            formatted_patch, was_patch_capped = cap_patch_content(formatted_patch, filename, max_patch_bytes)
            if was_patch_capped:
                capped_patches.append(filename)

        norm_name = _normalise_relative_posix(filename)
        prev_name = f.get("previous_filename", "")
        norm_prev = _normalise_relative_posix(prev_name) if prev_name else ""
        in_baseline = bool(baseline_set and (norm_name in baseline_set or (norm_prev and norm_prev in baseline_set)))

        raw_content: str | None = None
        if include_full_content and status not in ("added", "removed") and not in_baseline:
            if aggregate_limit == 0:
                disabled_by_zero = True
            else:
                loaded = fn_get_file_content(filename)
                if loaded:
                    raw_content = loaded
                    raw_bytes_len = len(loaded.encode("utf-8", errors="replace"))
                    eff_len = min(raw_bytes_len, limit) if limit > 0 else raw_bytes_len
                    full_indices.append(len(prepared))
                    effective_sizes.append(eff_len)

        prepared.append((f, formatted_patch, raw_content, in_baseline))

    if disabled_by_zero:
        print(
            "Diff full-file budget: max_diff_full_file_bytes=0 (disabled); omitting Full Current File Content blocks.",
            file=sys.stderr,
        )

    wf_cap: int | None = None
    if (
        aggregate_limit > 0
        and (aggregate_explicit or limit > 0)
        and effective_sizes
        and sum(effective_sizes) > aggregate_limit
    ):
        wf_cap = compute_waterfill_cap(effective_sizes, aggregate_limit)

    wf_capped: list[str] = []
    for idx in full_indices:
        entry_file, entry_patch, entry_raw, entry_in_baseline = prepared[idx]
        if not entry_raw:
            continue
        fname = entry_file["filename"]
        if wf_cap is not None and wf_cap > 0:
            target_cap = min(limit, wf_cap) if limit > 0 else wf_cap
        else:
            target_cap = limit
        capped_text, was_capped = cap_file_content(entry_raw, fname, target_cap)
        if was_capped:
            if wf_cap is not None and (limit <= 0 or wf_cap < limit):
                wf_capped.append(fname)
            else:
                capped.append(fname)
        prepared[idx] = (entry_file, entry_patch, capped_text, entry_in_baseline)

    if wf_capped and wf_cap is not None:
        wf_limit = min(limit, wf_cap) if limit > 0 else wf_cap
        print(
            f"Diff full-file budget: capped {len(wf_capped)} modified file(s) to {wf_limit:,} bytes each "
            f"(aggregate budget {aggregate_limit:,} bytes): {', '.join(wf_capped)}",
            file=sys.stderr,
        )

    if omitted:
        sample = ", ".join(omitted[:10])
        more = f", and {len(omitted) - 10} more" if len(omitted) > 10 else ""
        prompt_parts.append(
            f"Note: {len(omitted)} changed file(s) in this PR were automatically omitted from the text diff "
            f"(lockfiles, binaries, or generated assets): {sample}{more}. "
            "They ARE part of this PR — do not report them as missing or un-updated.\n"
        )

    for f, formatted_patch, full_content, in_baseline in prepared:
        filename = f["filename"]
        status = f["status"]
        prompt_parts.append(f"=== File: {filename} ===")
        prompt_parts.append(f"Status: {status}")
        prompt_parts.append("--- Diff (Patch) ---")
        prompt_parts.append(formatted_patch)
        if in_baseline and include_full_content and status not in ("added", "removed"):
            prompt_parts.append("Note: Pre-PR baseline content is already included in Repository Context above.")
        elif full_content:
            prompt_parts.append("--- Full Current File Content ---")
            prompt_parts.append(fn_format_file_content(full_content))
        prompt_parts.append("=========================\n")

    report_capped(capped, limit)
    if max_patch_bytes is not None and max_patch_bytes > 0:
        report_capped_patches(capped_patches, max_patch_bytes)
    return "\n".join(prompt_parts)


def build_prompt(
    files: list,
    config: dict,
    comment_history: str = "",
    client: Any = None,
    model: str | None = None,
    context_telemetry: dict[str, Any] | None = None,
    *,
    excluded_files: list[str] | None = None,
) -> str:
    """Consolidate file patches, PR comment history, and file contents into a single review context."""
    fn_build_pr_diff_prompt = _get_pr_review_func("build_pr_diff_prompt", build_pr_diff_prompt)
    fn_build_codebase_context = _get_pr_review_func("build_codebase_context", build_codebase_context)

    excluded_kwargs: dict[str, Any] = {"excluded_files": excluded_files} if excluded_files else {}
    if excluded_kwargs:
        try:
            diff_context = fn_build_pr_diff_prompt(files, config, **excluded_kwargs)
        except TypeError:
            diff_context = fn_build_pr_diff_prompt(files, config)
    else:
        diff_context = fn_build_pr_diff_prompt(files, config)

    try:
        codebase_context = fn_build_codebase_context(
            files,
            config,
            client=client,
            model=model,
            context_telemetry=context_telemetry,
            **excluded_kwargs,
        )
    except TypeError:
        try:
            codebase_context = fn_build_codebase_context(
                files,
                config,
                client=client,
                model=model,
                context_telemetry=context_telemetry,
            )
        except TypeError:
            # Fallback for custom or mocked fn_build_codebase_context that don't accept context_telemetry
            codebase_context = fn_build_codebase_context(files, config, client=client, model=model)

    parts = [diff_context]
    if comment_history:
        parts.append(comment_history)
    if codebase_context:
        parts.append(codebase_context)

    return "\n\n".join(parts)


def build_visual_diff_parts(
    image_files: list[dict],
    base_sha: str = "main",
    repository: str = "",
    headers: dict | None = None,
    config: dict | None = None,
    timeout: int = 60,
    head_sha: str = "HEAD",
) -> tuple[str, list[Any]]:
    """Generate visual diff summary text and binary types.Part objects for modified/added/removed image files."""
    if not image_files:
        return "", []

    cfg = config or {}
    max_images = get_max_multimodal_images(cfg)
    trigger_bytes = get_image_trigger_bytes(cfg)
    target_bytes = get_image_target_bytes(cfg)

    fn_get_git_blob = _get_pr_review_func("get_git_blob", get_git_blob)
    fn_get_file_blob = _get_pr_review_func("get_file_blob", get_file_blob)

    parts: list[Any] = []
    summary_lines: list[str] = []
    processed_count = 0

    for img_info in image_files:
        if processed_count >= max_images:
            print(
                f"Visual diff: reached max multimodal image limit ({max_images}). Skipping remaining images.",
                file=sys.stderr,
            )
            break

        fname = img_info.get("filename", "")
        status = img_info.get("status", "modified")
        if not fname or not is_supported_image(fname):
            continue

        head_bytes = None
        base_bytes = None

        # 1. Retrieve head bytes (unless removed)
        if status != "removed":
            if os.path.isfile(fname):
                try:
                    with open(fname, "rb") as f_head:
                        head_bytes = f_head.read()
                except Exception as e:
                    print(f"Warning: Failed to read local head image '{fname}': {e}", file=sys.stderr)
            if head_bytes is None:
                head_bytes = fn_get_git_blob(fname, head_sha)
            if head_bytes is None and repository and headers:
                head_bytes = fn_get_file_blob(repository, fname, head_sha, headers, timeout=timeout)

        # 2. Retrieve base bytes (for modified or removed)
        if status in ("modified", "removed"):
            if base_sha:
                base_bytes = fn_get_git_blob(fname, base_sha)
                if base_bytes is None and repository and headers:
                    base_bytes = fn_get_file_blob(repository, fname, base_sha, headers, timeout=timeout)
            if base_bytes is None:
                for fallback_ref in ("main", "master", "HEAD~1"):
                    base_bytes = fn_get_git_blob(fname, fallback_ref)
                    if base_bytes:
                        break

        # 3. Optimize bytes
        head_mime = get_mime_type(fname)
        base_mime = get_mime_type(fname)
        if head_bytes:
            head_bytes, head_mime = optimize_image_bytes(head_bytes, fname, trigger_bytes, target_bytes)
            if not is_gemini_supported_mime(head_mime):
                head_bytes = None
        if base_bytes:
            base_bytes, base_mime = optimize_image_bytes(base_bytes, fname, trigger_bytes, target_bytes)
            if not is_gemini_supported_mime(base_mime):
                base_bytes = None

        # 4. Assemble visual diff parts
        if status == "modified":
            if base_bytes and head_bytes:
                parts.append(f"=== Visual Diff: Modified Image '{fname}' ===")
                parts.append(f"[Visual Diff] Base (prior) version of '{fname}':")
                parts.append(types.Part.from_bytes(data=base_bytes, mime_type=base_mime))
                parts.append(f"[Visual Diff] Head (new) version of '{fname}':")
                parts.append(types.Part.from_bytes(data=head_bytes, mime_type=head_mime))
                summary_lines.append(f"- Modified image: {fname} (attached base and head visual comparison)")
                processed_count += 1
            elif head_bytes:
                parts.append(f"=== Visual Diff: Modified Image '{fname}' (base version unavailable) ===")
                parts.append(f"[Visual Diff] Head (current) version of '{fname}':")
                parts.append(types.Part.from_bytes(data=head_bytes, mime_type=head_mime))
                summary_lines.append(f"- Modified image: {fname} (attached head version, base unavailable)")
                processed_count += 1
        elif status == "added":
            if head_bytes:
                parts.append(f"=== Visual Diff: Added Image '{fname}' ===\n[Visual Diff] Added new image '{fname}':")
                parts.append(types.Part.from_bytes(data=head_bytes, mime_type=head_mime))
                summary_lines.append(f"- Newly added image: {fname} (attached new image)")
                processed_count += 1
        elif status == "removed":
            if base_bytes:
                parts.append(
                    f"=== Visual Diff: Removed Image '{fname}' ===\n[Visual Diff] Removed previous image '{fname}':"
                )
                parts.append(types.Part.from_bytes(data=base_bytes, mime_type=base_mime))
                summary_lines.append(f"- Removed image: {fname} (attached prior version)")
                processed_count += 1

    summary_text = "\n".join(summary_lines) if summary_lines else ""
    return summary_text, parts
