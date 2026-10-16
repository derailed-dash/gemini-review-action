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
    DEFAULT_MAX_REMOTE_IMAGE_BYTES,
    DEFAULT_MAX_TRIAGE_IMAGES,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MULTIMODAL_EXTENSIONS,
    build_triage_multimodal_parts,
    extract_markdown_image_references,
    extract_remote_image_urls,
    fetch_remote_image,
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
    "resolve_tool_calls_loop",
    # Multimodal re-exports
    "DEFAULT_MAX_REMOTE_IMAGE_BYTES",
    "DEFAULT_MAX_TRIAGE_IMAGES",
    "SUPPORTED_IMAGE_EXTENSIONS",
    "SUPPORTED_MULTIMODAL_EXTENSIONS",
    "is_supported_image",
    "is_supported_multimodal_file",
    "get_mime_type",
    "optimize_image_bytes",
    "extract_markdown_image_references",
    "extract_remote_image_urls",
    "fetch_remote_image",
    "build_triage_multimodal_parts",
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


def _has_usable_text(response: Any) -> bool:
    """Return True if the response has a non-empty text string."""
    if not response:
        return False
    try:
        text = getattr(response, "text", None)
        return isinstance(text, str) and bool(text.strip())
    except Exception:
        return False


def _extract_function_call_parts(response: Any) -> tuple[Any, list[Any]]:
    """Extract the model's Content object and any FunctionCall objects from a response."""
    model_content = None
    fc_list: list[Any] = []

    try:
        candidates = getattr(response, "candidates", None)
        if candidates and len(candidates) > 0:
            cand_content = getattr(candidates[0], "content", None)
            if cand_content is not None:
                model_content = cand_content
                parts = getattr(cand_content, "parts", None) or []
                for p in parts:
                    fc = getattr(p, "function_call", None)
                    if fc is not None and getattr(fc, "name", None):
                        fc_list.append(fc)
    except Exception:
        pass

    if not fc_list:
        try:
            raw_fcs = getattr(response, "function_calls", None)
            if raw_fcs:
                for fc in raw_fcs:
                    if getattr(fc, "name", None) or (isinstance(fc, dict) and fc.get("name")):
                        fc_list.append(fc)
        except Exception:
            pass

    return model_content, fc_list


def _accumulate_usage(accumulated: dict[str, int], response: Any) -> None:
    """Accumulate token usage metrics across multi-turn tool calls."""
    usage = getattr(response, "usage_metadata", None) if response else None
    if not usage:
        return
    for key in (
        "prompt_token_count",
        "cached_content_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
    ):
        raw = getattr(usage, key, 0)
        val = int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
        accumulated[key] = accumulated.get(key, 0) + val


def resolve_tool_calls_loop(
    client: Any,
    model_name: str,
    initial_contents: Any,
    gen_config: Any,
    tools: list[Any] | None,
    response: Any,
    fallback_contents: Any = None,
    system_instruction: str | None = None,
    billing_labels: dict[str, str] | None = None,
    thinking_cfg: Any = None,
    max_turns: int = 5,
) -> Any:
    """Resolve function_call-only responses and fall back to tool-free generation if needed.

    When Gemini is invoked with `cached_content` (where tool declarations are stored in the
    cache and `gen_config.tools` is None) or when `response_schema` + `tools` causes the SDK's
    automatic function calling loop to return a `function_call` part with no text (Issue #42),
    this loop executes the requested Python tool callables, sends the `function_response` parts
    back to the model, and repeats up to `max_turns`.

    If the response still lacks usable text after the tool loop (e.g. due to a safety/prohibited
    content block on a tool turn or exhausted turns), it falls back to a clean tool-free
    `generate_content` call so the review degrades gracefully instead of crashing the CI run.
    """
    from google.genai import types

    if _has_usable_text(response):
        return response

    function_map: dict[str, Any] = {}
    for tool_fn in tools or []:
        if callable(tool_fn):
            tool_name = getattr(tool_fn, "__name__", None)
            if isinstance(tool_name, str) and tool_name:
                # Resolve via _get_pr_review_func so unit test mocks on gemini_review/gemini_pr_review are respected
                function_map[tool_name] = _get_pr_review_func(tool_name, tool_fn)

    accumulated_usage: dict[str, int] = {}
    _accumulate_usage(accumulated_usage, response)

    # Normalise initial contents into a single user Content turn (or preserve existing Content list)
    if isinstance(initial_contents, types.Content):
        history: list[Any] = [initial_contents]
    elif isinstance(initial_contents, list) and all(isinstance(c, types.Content) for c in initial_contents):
        history = list(initial_contents)
    else:
        init_parts: list[Any] = []
        items = initial_contents if isinstance(initial_contents, (list, tuple)) else [initial_contents]
        for item in items:
            if isinstance(item, str):
                init_parts.append(types.Part.from_text(text=item))
            elif isinstance(item, types.Part):
                init_parts.append(item)
            elif isinstance(item, types.Content):
                init_parts.extend(getattr(item, "parts", None) or [])
        history = [types.Content(role="user", parts=init_parts)]

    turn = 0
    while not _has_usable_text(response) and turn < max_turns:
        model_content, fc_list = _extract_function_call_parts(response)
        if not fc_list or not function_map:
            break

        turn += 1
        response_parts: list[Any] = []
        model_call_parts: list[Any] = []

        for fc in fc_list:
            raw_name = getattr(fc, "name", None) if not isinstance(fc, dict) else fc.get("name")
            if not isinstance(raw_name, str) or not raw_name:
                continue
            fn_name: str = raw_name
            raw_args = getattr(fc, "args", None) if not isinstance(fc, dict) else fc.get("args")
            call_args = dict(raw_args) if isinstance(raw_args, dict) else {}

            if model_content is None:
                model_call_parts.append(types.Part.from_function_call(name=fn_name, args=call_args))

            func = function_map.get(fn_name)
            if func is None:
                func_result: dict[str, Any] = {"error": f"Unknown tool '{fn_name}'"}
            else:
                try:
                    func_result = {"result": func(**call_args)}
                except Exception as tool_err:
                    func_result = {"error": str(tool_err)}

            response_parts.append(types.Part.from_function_response(name=fn_name, response=func_result))

        if not response_parts:
            break

        if model_content is not None:
            history.append(model_content)
        elif model_call_parts:
            history.append(types.Content(role="model", parts=model_call_parts))

        history.append(types.Content(role="user", parts=response_parts))

        print(
            f"Resolving {len(fc_list)} tool call(s) (turn {turn}/{max_turns})...",
            file=sys.stderr,
        )
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=history,
                config=gen_config,
            )
            _accumulate_usage(accumulated_usage, response)
        except Exception as turn_err:
            print(
                f"Warning: Tool resolution turn {turn} failed ({turn_err}). Proceeding to tool-free fallback...",
                file=sys.stderr,
            )
            break

    # If the response still has no usable text (e.g. blocked on a tool turn, or model returned
    # function_call when tools were active, or exhausted max_turns), fall back to a tool-free review
    if not _has_usable_text(response) and (tools or getattr(gen_config, "cached_content", None)):
        # Log why the tool-enabled response had no text before falling back
        try:
            prompt_fb = getattr(response, "prompt_feedback", None)
            block_reason = getattr(prompt_fb, "block_reason", None) if prompt_fb else None
            if block_reason:
                print(
                    f"Warning: Tool-enabled response blocked (block_reason={block_reason}). "
                    "Falling back to review without tools...",
                    file=sys.stderr,
                )
            else:
                print(
                    "Warning: Model returned non-text response during tool execution. "
                    "Falling back to review without tools...",
                    file=sys.stderr,
                )
        except Exception:
            pass

        fallback_cfg = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=None,
            response_mime_type="application/json",
            response_schema=getattr(gen_config, "response_schema", None),
            labels=billing_labels,
            thinking_config=thinking_cfg,
        )
        target_contents = fallback_contents if fallback_contents is not None else initial_contents
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=target_contents,
                config=fallback_cfg,
            )
            _accumulate_usage(accumulated_usage, response)
        except Exception as fb_err:
            print(f"Warning: Tool-free fallback failed ({fb_err}).", file=sys.stderr)

    # Update usage_metadata on the final response if multiple turns occurred so cost telemetry
    # accurately reflects all tokens billed across the session.
    if (
        turn > 0
        and response is not None
        and getattr(response, "usage_metadata", None) is not None
        and accumulated_usage
    ):
        try:
            usage_obj = response.usage_metadata
            for key, total_val in accumulated_usage.items():
                if hasattr(usage_obj, key):
                    setattr(usage_obj, key, total_val)
        except Exception:
            pass

    return response
