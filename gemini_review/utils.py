"""General core primitives, token estimation, and facade utilities.

This module provides common primitives (token counting, response extraction,
workspace rules discovery), while re-exporting functions from the specialised
multimodal, diff, and repo modules to ensure full backward compatibility.
"""

import os
import sys
from typing import Any

# Re-export diff & patch parsing utilities
from gemini_review.diff import (
    _auto_align_suggestion_indentation,
    _auto_correct_suggestion_range,
    filter_review_comments,
    format_diff_patch_with_line_numbers,
    format_file_content_with_line_numbers,
    get_valid_changed_lines,
    get_valid_diff_lines,
    sanitize_code_suggestion,
)

# Re-export multimodal utilities
from gemini_review.multimodal import (
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MULTIMODAL_EXTENSIONS,
    extract_markdown_image_references,
    get_mime_type,
    is_supported_image,
    is_supported_multimodal_file,
    optimize_image_bytes,
)

# Re-export repository and filesystem utilities
from gemini_review.repo import (
    extract_import_references,
    generate_file_tree,
    get_all_repo_files,
    get_file_content,
    get_git_blob,
    get_local_git_files,
    is_core_file,
    is_text_file,
    rank_and_bound_candidates,
)

__all__ = [
    # Core utils primitives
    "_get_pr_review_func",
    "_normalize_model_name",
    "load_workspace_rules",
    "count_text_tokens",
    "extract_response_text_or_raise",
    # Multimodal re-exports
    "SUPPORTED_IMAGE_EXTENSIONS",
    "SUPPORTED_MULTIMODAL_EXTENSIONS",
    "is_supported_image",
    "is_supported_multimodal_file",
    "get_mime_type",
    "optimize_image_bytes",
    "extract_markdown_image_references",
    # Diff re-exports
    "format_file_content_with_line_numbers",
    "format_diff_patch_with_line_numbers",
    "get_valid_diff_lines",
    "get_valid_changed_lines",
    "_auto_correct_suggestion_range",
    "_auto_align_suggestion_indentation",
    "sanitize_code_suggestion",
    "filter_review_comments",
    # Repo re-exports
    "is_text_file",
    "get_file_content",
    "get_local_git_files",
    "get_git_blob",
    "get_all_repo_files",
    "is_core_file",
    "generate_file_tree",
    "extract_import_references",
    "rank_and_bound_candidates",
]


def _get_pr_review_func(name: str, fallback: Any) -> Any:
    """Retrieve function from gemini_review or gemini_pr_review if patched to support test mocks."""
    for mod_name in (
        "gemini_pr_review",
        "gemini_review.prompts",
        "gemini_review.context",
        "gemini_review.diff",
        "gemini_review.repo",
        "gemini_review.utils",
        "gemini_review",
    ):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, name):
            val = getattr(mod, name)
            if val is not fallback:
                return val
    return fallback


def _normalize_model_name(model: str | None) -> str:
    """Normalise model string by stripping leading 'models/' or publisher prefixes and converting to lowercase."""
    if not model:
        return ""
    name = model.strip().lower()
    if "models/" in name:
        name = name.split("models/")[-1]
    return name


def load_workspace_rules() -> str:
    """Check for workspace rule files (.agents/AGENTS.md, AGENTS.md, etc.) and return their combined contents."""
    possible_paths = [".agents/AGENTS.md", "AGENTS.md", ".agents/GEMINI.md", "GEMINI.md"]
    rules_content = []
    for path in possible_paths:
        if os.path.exists(path) and os.path.isfile(path):
            try:
                print(f"Loading workspace rules from {path}...", file=sys.stderr)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        rules_content.append(f"=== Rules from {path} ===\n{content}\n")
            except Exception as e:
                print(f"Warning: Failed to load workspace rules from {path}: {e}", file=sys.stderr)

    return "\n".join(rules_content) if rules_content else ""


def count_text_tokens(client, model_name: str, text: str | list | tuple) -> int:
    """Count or estimate the number of tokens in a text string or multimodal contents list."""
    if not text:
        return 0
    if client and hasattr(client, "models") and hasattr(client.models, "count_tokens"):
        try:
            resp = client.models.count_tokens(model=model_name, contents=text)
            total = getattr(resp, "total_tokens", None)
            # Must be a real int. A bool is an int in Python and a stub/mock client can
            # return anything at all; either would be used in arithmetic and formatting
            # downstream, so an unusable value falls through to the estimate rather than
            # raising inside a review that is about to be posted.
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                return total
        except Exception:
            pass
    # Fallback heuristic (~4 chars per token for text, ~258 tokens per multimodal part)
    if isinstance(text, (list, tuple)):
        est = 0
        for item in text:
            if isinstance(item, str):
                est += len(item) // 4
            else:
                est += 258
        return max(1, est)
    return max(1, len(text) // 4)


def extract_response_text_or_raise(response: Any) -> str:
    """Extract text content from a Gemini model response or raise RuntimeError with detailed diagnostics.

    Inspects candidates, finish reasons, block reasons, and function call attempts when response.text is None.
    Safely wraps property accesses in try-except to catch ValueError or AttributeError raised by SDK getters.
    """
    text = None
    try:
        text = getattr(response, "text", None)
    except Exception:
        text = None

    if text:
        return text

    diag_lines = ["Gemini model returned empty or non-text response."]

    candidates = None
    try:
        candidates = getattr(response, "candidates", None)
    except Exception:
        candidates = None

    if candidates:
        for idx, candidate in enumerate(candidates):
            try:
                finish_reason = getattr(candidate, "finish_reason", "UNKNOWN")
            except Exception:
                finish_reason = "UNKNOWN"

            try:
                finish_msg = getattr(candidate, "finish_message", None)
            except Exception:
                finish_msg = None

            msg_str = f" ({finish_msg})" if finish_msg else ""
            diag_lines.append(f"Candidate {idx}: finish_reason={finish_reason}{msg_str}")

            try:
                safety_ratings = getattr(candidate, "safety_ratings", None)
            except Exception:
                safety_ratings = None

            if safety_ratings:
                diag_lines.append(f"Candidate {idx} safety ratings: {safety_ratings}")

    try:
        function_calls = getattr(response, "function_calls", None)
    except Exception:
        function_calls = None

    if function_calls:
        diag_lines.append(f"Model emitted function call(s) instead of text: {function_calls}")

    try:
        prompt_feedback = getattr(response, "prompt_feedback", None)
    except Exception:
        prompt_feedback = None

    if prompt_feedback:
        try:
            block_reason = getattr(prompt_feedback, "block_reason", None)
        except Exception:
            block_reason = None

        if block_reason:
            diag_lines.append(f"Prompt blocked: block_reason={block_reason}")

    error_msg = "\n".join(diag_lines)
    print(f"Error: {error_msg}", file=sys.stderr)
    raise RuntimeError(error_msg)
