"""Dynamic context selection and hybrid codebase context engine for Gemini PR reviews.

This module assesses repository size to select between Full Context Mode (embedding all text files)
and Sparse Context Mode (file tree, core documents/manifests, and LLM dynamic candidate selection).
It also extracts and attaches local Markdown image references as multimodal parts.
"""

import fnmatch
import inspect
import json
import os
import sys
from typing import Any

from google.genai import types

from gemini_review.budget import cap_file_content, max_file_bytes, report_capped
from gemini_review.config import (
    build_thinking_config,
    get_attach_markdown_images,
    get_context_diff_directories_only,
    get_context_exclude_patterns,
    get_context_mode,
    get_default_model,
    get_extra_context_files,
    get_image_target_bytes,
    get_image_trigger_bytes,
    get_max_candidate_files,
    get_max_context_bytes,
    get_max_multimodal_images,
)
from gemini_review.multimodal import (
    extract_markdown_image_references,
    is_gemini_supported_mime,
    is_supported_multimodal_file,
    optimize_image_bytes,
)
from gemini_review.repo import (
    _normalise_relative_posix,
    ensure_git_ref_available,
    generate_file_tree,
    get_all_repo_files,
    get_file_content,
    get_git_blob,
    is_core_file,
    is_text_file,
    list_git_tree_files,
    matches_exclude_patterns,
    rank_and_bound_candidates,
    read_git_tree_texts,
)
from gemini_review.schemas import DynamicContextSelection
from gemini_review.utils import (
    _get_pr_review_func,
    generate_content_with_stream_fallback,
    is_thinking_config_error,
)


def select_dynamic_context_files(
    client: Any,
    model: str,
    files: list[dict],
    candidate_files: list[str],
    max_files: int = 8,
    thinking_level: str | int | None = None,
    *,
    service_tier: str = "standard",
    service_tier_fallback: bool = True,
) -> tuple[list[str], str, dict[str, Any]]:
    """Dynamically select the most relevant repository files for PR review context."""
    if not client or not candidate_files:
        return [], "", {}

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

    # Context selector defaults to 'low' thinking to prevent token waste
    eff_thinking_level = (
        thinking_level if thinking_level is not None else (os.environ.get("GEMINI_THINKING_LEVEL") or "low")
    )
    thinking_cfg = build_thinking_config(eff_thinking_level, model=model)

    try:
        print(
            f"Dynamic context selection: evaluating {len(candidate_files)} candidate files with '{model}'...",
            file=sys.stderr,
        )

        sdk_service_tier = types.ServiceTier.FLEX if (service_tier or "standard").strip().lower() == "flex" else None
        gen_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=DynamicContextSelection,
            thinking_config=thinking_cfg,
            service_tier=sdk_service_tier,
        )

        try:
            response = generate_content_with_stream_fallback(
                client,
                model=model,
                contents=prompt,
                config=gen_config,
                service_tier_fallback=service_tier_fallback,
            )
        except Exception as api_err:
            # If the model rejects thinking_config, retry without thinking_config for backward/forward compatibility
            if thinking_cfg is not None and is_thinking_config_error(api_err):
                print(
                    f"Warning: thinking_config not supported by model '{model}' ({api_err}). "
                    "Retrying without thinking_config...",
                    file=sys.stderr,
                )
                gen_config.thinking_config = None
                response = generate_content_with_stream_fallback(
                    client,
                    model=model,
                    contents=prompt,
                    config=gen_config,
                    service_tier_fallback=service_tier_fallback,
                )
            else:
                raise

        usage_dict: dict[str, Any] = {
            "prompt_tokens": 0,
            "candidates_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
        }
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            u = response.usage_metadata
            usage_dict["prompt_tokens"] = getattr(u, "prompt_token_count", 0) or 0
            usage_dict["candidates_tokens"] = getattr(u, "candidates_token_count", 0) or 0
            usage_dict["thoughts_tokens"] = getattr(u, "thoughts_token_count", 0) or 0
            usage_dict["total_tokens"] = getattr(u, "total_token_count", 0) or (
                usage_dict["prompt_tokens"] + usage_dict["candidates_tokens"] + usage_dict["thoughts_tokens"]
            )

        raw_text = getattr(response, "text", "") or "{}"
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
        return valid_selected, selection.reasoning, usage_dict
    except Exception as e:
        print(f"Warning: Dynamic context selection failed ({e}). Proceeding without dynamic files.", file=sys.stderr)
        return [], "", {}


def _extract_patch_added_lines(patch: str) -> str:
    """Return only added (`+`) lines from a unified diff patch, stripping the leading `+` marker."""
    if not patch:
        return ""
    added: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added.append(line[1:])
    return "\n".join(added)


def _is_core_diagram_doc(md_path: str) -> bool:
    """Return True if a Markdown path is an architecture, design, or specification document."""
    norm = _normalise_relative_posix(md_path).lower()
    base = os.path.basename(norm)
    for pat in ("architecture*", "design*", "spec*", "docs/architecture*", "docs/design*", "docs/spec*"):
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(base, pat):
            return True
    return False


def _should_attach_background_md_images(attach_mode: str, md_path: str) -> bool:
    """Decide whether a background repository Markdown document may attach linked images."""
    if attach_mode == "all":
        return True
    if attach_mode == "core":
        return _is_core_diagram_doc(md_path)
    return False


def build_codebase_context(
    files: list,
    config: dict,
    client: Any = None,
    model: str | None = None,
    context_telemetry: dict[str, Any] | None = None,
    multimodal_parts: list[Any] | None = None,
    *,
    excluded_files: list[str] | None = None,
    base_sha: str | None = None,
    dynamic_multimodal_parts: list[Any] | None = None,
    service_tier: str = "standard",
    service_tier_fallback: bool = True,
) -> str:
    """Build the repository codebase context (Full baseline, Full, Sparse, or Diff-only) for PR review."""
    fn_get_all_repo_files = _get_pr_review_func("get_all_repo_files", get_all_repo_files)
    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    fn_is_core_file = _get_pr_review_func("is_core_file", is_core_file)
    fn_generate_file_tree = _get_pr_review_func("generate_file_tree", generate_file_tree)
    fn_select_dynamic_context_files = _get_pr_review_func("select_dynamic_context_files", select_dynamic_context_files)
    fn_is_text_file = _get_pr_review_func("is_text_file", is_text_file)
    fn_ensure_git_ref_available = _get_pr_review_func("ensure_git_ref_available", ensure_git_ref_available)
    fn_list_git_tree_files = _get_pr_review_func("list_git_tree_files", list_git_tree_files)
    fn_read_git_tree_texts = _get_pr_review_func("read_git_tree_texts", read_git_tree_texts)
    fn_get_git_blob = _get_pr_review_func("get_git_blob", get_git_blob)

    attach_mode = get_attach_markdown_images(config)
    context_mode = get_context_mode(config)
    max_context_bytes = get_max_context_bytes(config)
    file_byte_limit = max_file_bytes(config)
    context_exclude_patterns = get_context_exclude_patterns(config)
    extra_files = get_extra_context_files(config)

    max_images = get_max_multimodal_images(config)
    trigger_bytes = get_image_trigger_bytes(config)
    target_bytes = get_image_target_bytes(config)
    seen_images: set[str] = set()

    def _attach_markdown_images(
        md_content: str,
        md_path: str,
        *,
        target_parts: list[Any] | None = None,
        git_ref: str | None = None,
    ) -> None:
        if attach_mode == "none":
            return
        dest_parts = multimodal_parts if target_parts is None else target_parts
        if dest_parts is None or len(seen_images) >= max_images:
            return
        refs = extract_markdown_image_references(md_content, md_path)
        for img_ref in refs:
            if img_ref in seen_images or len(seen_images) >= max_images:
                continue
            raw_bytes: bytes | None = None
            if git_ref:
                raw_bytes = fn_get_git_blob(img_ref, git_ref)
            else:
                if not os.path.isfile(img_ref):
                    continue
                try:
                    with open(img_ref, "rb") as f_img:
                        raw_bytes = f_img.read()
                except Exception as e:
                    print(f"Warning: Failed to attach image '{img_ref}' ({e}).", file=sys.stderr)
                    continue
            if not raw_bytes:
                continue
            try:
                opt_bytes, mime = optimize_image_bytes(
                    raw_bytes, img_ref, trigger_bytes=trigger_bytes, target_bytes=target_bytes
                )
                if not is_gemini_supported_mime(mime):
                    print(
                        f"Warning: Skipping image '{img_ref}' referenced in '{md_path}' "
                        f"(unsupported Gemini MIME type '{mime}').",
                        file=sys.stderr,
                    )
                    continue
                part = types.Part.from_bytes(data=opt_bytes, mime_type=mime)
                dest_parts.append(f"Visual Context (referenced in '{md_path}'): {img_ref}")
                dest_parts.append(part)
                seen_images.add(img_ref)
                print(
                    f"Multimodal context: attached image '{img_ref}' referenced in '{md_path}' "
                    f"({len(opt_bytes):,} bytes).",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"Warning: Failed to attach image '{img_ref}' ({e}).", file=sys.stderr)

    def _scan_pr_diff_markdown_images() -> None:
        if attach_mode == "none":
            return
        tail_parts = dynamic_multimodal_parts if dynamic_multimodal_parts is not None else multimodal_parts
        for f in files:
            fname = f.get("filename", "") if isinstance(f, dict) else str(f)
            if not fname.lower().endswith((".md", ".markdown")):
                continue
            if attach_mode == "all":
                md_content = fn_get_file_content(fname)
                if md_content:
                    _attach_markdown_images(md_content, fname, target_parts=tail_parts)
            else:
                patch = f.get("patch", "") if isinstance(f, dict) else ""
                added_lines = _extract_patch_added_lines(patch)
                if added_lines:
                    _attach_markdown_images(added_lines, fname, target_parts=tail_parts)

    prompt_parts: list[str] = []
    pr_filenames = {f["filename"] if isinstance(f, dict) else str(f) for f in files}
    skip_filenames = {_normalise_relative_posix(p) for p in pr_filenames}
    if excluded_files:
        skip_filenames.update(_normalise_relative_posix(excl) for excl in excluded_files)

    def _attach_extra_files() -> None:
        prompt_parts.append("--- Relevant Codebase Context (Static Extra Files) ---")
        extra_capped: list[str] = []
        for ef in extra_files:
            if not os.path.exists(ef):
                continue
            if is_supported_multimodal_file(ef):
                prompt_parts.append(
                    f"--- Multimodal File: {ef} (binary content attached directly to Gemini request) ---"
                )
                if multimodal_parts is not None and len(seen_images) < max_images and ef not in seen_images:
                    try:
                        with open(ef, "rb") as f_bin:
                            raw_bytes = f_bin.read()
                        if raw_bytes:
                            opt_bytes, mime = optimize_image_bytes(
                                raw_bytes, ef, trigger_bytes=trigger_bytes, target_bytes=target_bytes
                            )
                            if not is_gemini_supported_mime(mime):
                                print(
                                    f"Warning: Skipping extra multimodal file '{ef}' "
                                    f"(unsupported Gemini MIME type '{mime}').",
                                    file=sys.stderr,
                                )
                                continue
                            part = types.Part.from_bytes(data=opt_bytes, mime_type=mime)
                            multimodal_parts.append(f"Extra Context File: {ef}")
                            multimodal_parts.append(part)
                            seen_images.add(ef)
                    except Exception as e:
                        print(f"Warning: Failed to attach extra multimodal file '{ef}': {e}", file=sys.stderr)
                continue

            content = fn_get_file_content(ef)
            if content:
                content, was_capped = cap_file_content(content, ef, file_byte_limit)
                if was_capped:
                    extra_capped.append(ef)
                prompt_parts.append(f"--- File: {ef} ---")
                prompt_parts.append(content)
                prompt_parts.append("-----------------\n")
                if attach_mode != "none" and ef.lower().endswith((".md", ".markdown")):
                    _attach_markdown_images(content, ef)
        report_capped(extra_capped, file_byte_limit)

    if context_mode == "diff_only":
        print("Codebase context: running in Diff-Only Mode (skipping repository background context).", file=sys.stderr)
        if context_telemetry is not None and isinstance(context_telemetry, dict):
            context_telemetry["context_mode"] = "diff_only"
            context_telemetry["baseline_included"] = False
        _scan_pr_diff_markdown_images()
        if extra_files:
            _attach_extra_files()
        return "\n".join(prompt_parts)

    # Attempt invariant base_sha baseline Full Context Mode before working-tree enumeration so
    # multiple PRs and successive commits share a byte-identical prefix.
    can_try_baseline = (
        bool(base_sha)
        and context_mode in ("auto", "full")
        and (fn_get_all_repo_files is get_all_repo_files or fn_list_git_tree_files is not list_git_tree_files)
    )
    if can_try_baseline and base_sha and fn_ensure_git_ref_available(base_sha):
        tree_entries = fn_list_git_tree_files(base_sha)
        baseline_entries = [
            (path, size)
            for path, size in tree_entries
            if fn_is_text_file(path) and not matches_exclude_patterns(path, context_exclude_patterns)
        ]
        baseline_total_size = sum(size for _, size in baseline_entries)
        if baseline_entries and (context_mode == "full" or baseline_total_size <= max_context_bytes):
            print(
                f"Codebase context: running in Invariant Baseline Full Context Mode "
                f"({len(baseline_entries)} files, {baseline_total_size:,} bytes at base ref).",
                file=sys.stderr,
            )
            baseline_texts = fn_read_git_tree_texts(base_sha, [p for p, _ in baseline_entries])
            prompt_parts.append("=== Repository Context (Full Codebase Baseline) ===")
            prompt_parts.append(
                "Below are the tracked repository files at the pre-PR baseline commit for codebase context "
                "(apply the Pull Request diffs in the section below to see the proposed changes):\n"
            )
            baseline_capped: list[str] = []
            included_baseline_files: set[str] = set()
            for path, _ in baseline_entries:
                content = baseline_texts.get(path)
                if not content:
                    continue
                content, was_capped = cap_file_content(content, path, file_byte_limit)
                if was_capped:
                    baseline_capped.append(path)
                prompt_parts.append(f"--- File: {path} ---")
                prompt_parts.append(content)
                prompt_parts.append("-----------------\n")
                included_baseline_files.add(_normalise_relative_posix(path))
                if path.lower().endswith((".md", ".markdown")) and _should_attach_background_md_images(
                    attach_mode, path
                ):
                    _attach_markdown_images(content, path, target_parts=multimodal_parts, git_ref=base_sha)
            report_capped(baseline_capped, file_byte_limit)
            prompt_parts.append("=========================================\n")

            if extra_files:
                _attach_extra_files()
            # Scan PR-diff Markdown additions AFTER the invariant prefix into the dynamic tail.
            _scan_pr_diff_markdown_images()

            if context_telemetry is not None and isinstance(context_telemetry, dict):
                context_telemetry["context_mode"] = "full_baseline"
                context_telemetry["baseline_included"] = True
                context_telemetry["baseline_included_files"] = included_baseline_files
            return "\n".join(prompt_parts)

    # Working-tree fallback (when base_sha is absent/unavailable, sparse mode is chosen, or
    # unit tests patch get_all_repo_files directly).
    _scan_pr_diff_markdown_images()

    max_core_context_bytes = config.get("max_core_context_bytes", 500 * 1024)
    if "GEMINI_MAX_CORE_CONTEXT_BYTES" in os.environ:
        try:
            max_core_context_bytes = int(os.environ["GEMINI_MAX_CORE_CONTEXT_BYTES"])
        except ValueError:
            pass

    core_patterns = config.get(
        "core_file_patterns",
        [
            # Guidelines & instruction files (always top priority)
            "AGENTS.md",
            "GEMINI.md",
            "CLAUDE.md",
            ".agents/AGENTS.md",
            ".agents/GEMINI.md",
            ".agents/CLAUDE.md",
            # Project overview & guidelines
            "README*",
            "CONTRIBUTING*",
            "ARCHITECTURE*",
            "DESIGN*",
            "SPEC*",
            "DEPLOYMENT*",
            "INSTALL*",
            "PRODUCT*",
            "SDD*",
            "TDD*",
            "TODO*",
            # Package manifests & build configs
            "pyproject.toml",
            "package.json",
            "go.mod",
            "Cargo.toml",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "Gemfile",
            "composer.json",
            "*.csproj",
            "*.sln",
            "CMakeLists.txt",
            "Makefile",
            "Dockerfile",
            "docker-compose*.yml",
            "action.yml",
            # Core shared utilities, base templates, and entrypoints
            "*template*",
            "*shared*",
            "*util*",
            "*common*",
            "*core*",
        ],
    )

    all_files = fn_get_all_repo_files()
    other_files = [
        f
        for f in all_files
        if _normalise_relative_posix(f) not in skip_filenames
        and fn_is_text_file(f)
        and not matches_exclude_patterns(f, context_exclude_patterns)
    ]

    if not other_files and not extra_files:
        return ""

    total_size = 0
    for f in other_files:
        try:
            total_size += os.path.getsize(f)
        except Exception:
            pass

    print(
        f"Codebase context: found {len(other_files)} additional repository files (total size {total_size} bytes,"
        f" limit {max_context_bytes} bytes).",
        file=sys.stderr,
    )

    use_full = context_mode == "full" or (context_mode == "auto" and total_size <= max_context_bytes)
    if use_full:
        if context_telemetry is not None and isinstance(context_telemetry, dict):
            context_telemetry["context_mode"] = "full"
            context_telemetry["baseline_included"] = False
        print("Codebase context: running in Full Context Mode (attaching all repository text files).", file=sys.stderr)
        prompt_parts.append("=== Repository Context (Full Codebase) ===")
        prompt_parts.append("Below are the contents of all other files in this repository for context:\n")
        full_capped: list[str] = []
        for f in other_files:
            content = fn_get_file_content(f)
            if content:
                content, was_capped = cap_file_content(content, f, file_byte_limit)
                if was_capped:
                    full_capped.append(f)
                prompt_parts.append(f"--- File: {f} ---")
                prompt_parts.append(content)
                prompt_parts.append("-----------------\n")
                if f.lower().endswith((".md", ".markdown")) and _should_attach_background_md_images(attach_mode, f):
                    _attach_markdown_images(content, f)
        report_capped(full_capped, file_byte_limit)
        prompt_parts.append("=========================================\n")

        # In Full Context Mode, also attach any explicit extra_context_files
        if extra_files:
            _attach_extra_files()
    else:
        if context_telemetry is not None and isinstance(context_telemetry, dict):
            context_telemetry["context_mode"] = "sparse"
            context_telemetry["baseline_included"] = False
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

        visible_repo_files = {
            f for f in all_files if fn_is_text_file(f) and not matches_exclude_patterns(f, context_exclude_patterns)
        }
        full_tree_files = list(pr_filenames.union(visible_repo_files))
        file_tree = fn_generate_file_tree(full_tree_files)
        prompt_parts.append("--- Repository File Structure ---")
        prompt_parts.append(file_tree)
        prompt_parts.append("---------------------------------\n")

        prompt_parts.append("--- Key Configuration and Documentation Files ---")
        core_files_included = []
        core_capped: list[str] = []
        core_bytes_used = 0

        # Prioritise standard agent rules and architectural documentation first
        priority_core_patterns = [
            "README*",
            "AGENTS.md",
            ".github/AGENTS.md",
            "GEMINI.md",
            ".github/GEMINI.md",
            "CLAUDE.md",
            ".github/CLAUDE.md",
            "docs/architecture*",
        ]

        def _core_sort_key(file_path: str) -> tuple[int, str]:
            norm_p = file_path.replace("\\", "/").removeprefix("./")
            for idx, pat in enumerate(priority_core_patterns):
                if fnmatch.fnmatch(norm_p, pat) or fnmatch.fnmatch(os.path.basename(norm_p), pat):
                    return (idx, norm_p)
            return (len(priority_core_patterns), norm_p)

        candidate_core_files = sorted([f for f in other_files if fn_is_core_file(f, core_patterns)], key=_core_sort_key)
        for f in candidate_core_files:
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
                    if f.lower().endswith((".md", ".markdown")) and _should_attach_background_md_images(attach_mode, f):
                        _attach_markdown_images(content, f)
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

        # Check for static extra_context_files bypass
        if extra_files:
            print(
                f"Codebase context: attaching {len(extra_files)} static extra context file(s)...",
                file=sys.stderr,
            )
            _attach_extra_files()
        else:
            # Dynamic context selection via model
            dynamic_candidates = [f for f in other_files if f not in core_files_included]
            if client and dynamic_candidates:
                max_candidates = get_max_candidate_files(config)
                diff_dirs_only = get_context_diff_directories_only(config)
                orig_count = len(dynamic_candidates)
                bounded_candidates = rank_and_bound_candidates(
                    dynamic_candidates,
                    files,
                    max_candidates=max_candidates,
                    diff_dirs_only=diff_dirs_only,
                    exclude_patterns=context_exclude_patterns,
                )
                if len(bounded_candidates) < orig_count:
                    print(
                        f"Dynamic context selection: bounded candidate pool from {orig_count} "
                        f"to {len(bounded_candidates)} files (max: {max_candidates}).",
                        file=sys.stderr,
                    )

                effective_model = get_default_model(model)
                sel_kwargs: dict[str, Any] = {}
                if (service_tier or "standard").strip().lower() == "flex":
                    sel_side_eff = getattr(fn_select_dynamic_context_files, "side_effect", None)
                    sel_target = sel_side_eff if callable(sel_side_eff) else fn_select_dynamic_context_files
                    try:
                        sel_params = inspect.signature(sel_target).parameters
                    except (TypeError, ValueError):
                        sel_params = {}
                    sel_accepts_var = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sel_params.values())
                    if sel_accepts_var or "service_tier" in sel_params:
                        sel_kwargs["service_tier"] = service_tier
                    if sel_accepts_var or "service_tier_fallback" in sel_params:
                        sel_kwargs["service_tier_fallback"] = service_tier_fallback

                res = fn_select_dynamic_context_files(
                    client=client,
                    model=effective_model,
                    files=files,
                    candidate_files=bounded_candidates,
                    **sel_kwargs,
                )
                if isinstance(res, tuple) and len(res) == 3:
                    selected_files, reasoning, sel_usage = res
                elif isinstance(res, tuple) and len(res) == 2:
                    selected_files, reasoning = res
                    sel_usage = {}
                else:
                    selected_files, reasoning, sel_usage = [], "", {}

                if context_telemetry is not None and isinstance(context_telemetry, dict):
                    context_telemetry["model"] = effective_model
                    context_telemetry["selected_files"] = selected_files
                    context_telemetry["reasoning"] = reasoning
                    if isinstance(sel_usage, dict):
                        context_telemetry.update(sel_usage)

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
                            if sf.lower().endswith((".md", ".markdown")) and _should_attach_background_md_images(
                                attach_mode, sf
                            ):
                                _attach_markdown_images(content, sf)
                    report_capped(dynamic_capped, file_byte_limit)

        prompt_parts.append("==========================================\n")

    return "\n".join(prompt_parts)
