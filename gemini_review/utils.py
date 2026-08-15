"""
Description: General file, git, diff, and text manipulation utilities.
Provides functions for filtering binary files, parsing diff patches,
generating repo file trees, and loading workspace agent rules.
"""

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Any

from gemini_review.schemas import InlineComment, ReviewResult


def _get_pr_review_func(name: str, fallback: Any) -> Any:
    """Retrieve function from gemini_pr_review module if present to support test mocks."""
    mod = sys.modules.get("gemini_pr_review")
    if mod and hasattr(mod, name):
        return getattr(mod, name)
    return fallback


def is_text_file(filename: str) -> bool:
    """Filter out typical binary, lock, and encrypted file formats."""
    excluded_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".enc",
        ".lock",
        ".db",
        ".pyc",
        ".o",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
    }
    _, ext = os.path.splitext(filename.lower())
    if ext in excluded_extensions:
        return False

    excluded_names = {"package-lock.json", "uv.lock", ".env", ".env.enc", ".envrc"}
    if os.path.basename(filename) in excluded_names:
        return False

    return True


def _normalize_model_name(model: str | None) -> str:
    """Normalise model string by stripping leading 'models/' or publisher prefixes and converting to lowercase."""
    if not model:
        return ""
    name = model.strip().lower()
    if "models/" in name:
        name = name.split("models/")[-1]
    return name


def format_file_content_with_line_numbers(content: str) -> str:
    """Format full file content with 1-based line number prefixes."""
    if not content:
        return ""
    lines = content.splitlines()
    width = max(len(str(len(lines))), 4)
    return "\n".join(f"{idx:{width}d} | {line}" for idx, line in enumerate(lines, start=1))


def format_diff_patch_with_line_numbers(patch: str) -> str:
    """Annotate unified diff patch lines with their corresponding line numbers."""
    if not patch:
        return ""

    annotated_lines = []
    current_old = 0
    current_new = 0

    for line in patch.splitlines():
        if line.startswith("@@"):
            annotated_lines.append(line)
            try:
                parts = line.split()
                old_info = parts[1].lstrip("-")
                new_info = parts[2].lstrip("+")
                current_old = int(old_info.split(",")[0])
                current_new = int(new_info.split(",")[0])
            except Exception:
                current_old = 0
                current_new = 0
        elif current_new == 0 and current_old == 0:
            annotated_lines.append(line)
        elif line.startswith("+"):
            if current_new > 0:
                annotated_lines.append(f"{current_new:5d} + | {line[1:]}")
                current_new += 1
            else:
                annotated_lines.append(line)
        elif line.startswith("-"):
            if current_old > 0:
                annotated_lines.append(f"{current_old:5d} - | {line[1:]}")
                current_old += 1
            else:
                annotated_lines.append(line)
        elif line.startswith(" ") or line == "":
            raw_text = line[1:] if line.startswith(" ") else ""
            if current_new > 0:
                annotated_lines.append(f"{current_new:5d}   | {raw_text}")
                current_new += 1
                current_old += 1
            else:
                annotated_lines.append(line)
        else:
            annotated_lines.append(line)

    return "\n".join(annotated_lines)


def get_valid_diff_lines(patch: str) -> tuple[set[int], set[int]]:
    """Parse the diff patch to find all valid line numbers for RIGHT side (new file) and LEFT side (old file)."""
    valid_right = set()
    valid_left = set()
    if not patch:
        return valid_right, valid_left

    current_old = 0
    current_new = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            try:
                parts = line.split()
                old_info = parts[1].lstrip("-")
                new_info = parts[2].lstrip("+")
                current_old = int(old_info.split(",")[0])
                current_new = int(new_info.split(",")[0])
            except Exception:
                current_old = 0
                current_new = 0
        elif line.startswith("+"):
            if current_new > 0:
                valid_right.add(current_new)
                current_new += 1
        elif line.startswith("-"):
            if current_old > 0:
                valid_left.add(current_old)
                current_old += 1
        elif line.startswith(" ") or line == "":
            if current_new > 0:
                valid_right.add(current_new)
                current_new += 1
            if current_old > 0:
                valid_left.add(current_old)
                current_old += 1
    return valid_right, valid_left


def get_valid_changed_lines(patch: str) -> set[int]:
    """Parse the diff patch to find all line numbers in the new file (RIGHT side) that are part of the diff."""
    valid_right, _ = get_valid_diff_lines(patch)
    return valid_right


def _auto_correct_suggestion_range(
    comment: InlineComment,
    matched_file: str,
    valid_set: set[int],
) -> None:
    """Auto-correct comment.start_line and comment.line if code_suggestion includes multi-line original file content."""
    if not comment.code_suggestion:
        return

    s_lines = [sl.strip() for sl in comment.code_suggestion.strip().splitlines() if sl.strip()]
    if len(s_lines) <= 1:
        return

    content = get_file_content(matched_file)
    if not content:
        return

    file_lines = {idx: line.strip() for idx, line in enumerate(content.splitlines(), start=1)}

    target_line = comment.line

    if comment.start_line is None:
        target_str = file_lines.get(target_line, "")
        if not target_str:
            return

        match_indices = [idx for idx, sl in enumerate(s_lines) if sl == target_str]
        if not match_indices:
            return

        min_line = target_line
        max_line = target_line

        idx_match = match_indices[0]

        # Check subsequent lines for contiguous sequential match
        offset = 1
        while (
            (target_line + offset) in file_lines
            and (target_line + offset) in valid_set
            and (idx_match + offset) < len(s_lines)
        ):
            if file_lines[target_line + offset] == s_lines[idx_match + offset]:
                max_line = target_line + offset
                offset += 1
            else:
                break

        # Check preceding lines for contiguous sequential match
        offset = 1
        while (
            (target_line - offset) in file_lines and (target_line - offset) in valid_set and (idx_match - offset) >= 0
        ):
            if file_lines[target_line - offset] == s_lines[idx_match - offset]:
                min_line = target_line - offset
                offset += 1
            else:
                break

        if min_line < max_line:
            comment.start_line = min_line
            comment.line = max_line


def _auto_align_suggestion_indentation(comment: InlineComment, matched_file: str) -> None:
    """Ensure comment.code_suggestion retains the base indentation of the target file line."""
    if not comment.code_suggestion:
        return

    content = get_file_content(matched_file)
    if not content:
        return

    file_lines = content.splitlines()
    target_line_idx = (comment.start_line or comment.line) - 1
    if not (0 <= target_line_idx < len(file_lines)):
        return

    target_line = file_lines[target_line_idx]
    target_indent = len(target_line) - len(target_line.lstrip(" \t"))
    if target_indent == 0:
        return

    indent_prefix = target_line[:target_indent]

    s_lines = comment.code_suggestion.splitlines()
    if not s_lines:
        return

    first_s_indent = len(s_lines[0]) - len(s_lines[0].lstrip(" \t"))
    if first_s_indent < target_indent:
        delta = target_indent - first_s_indent
        indent_addition = indent_prefix[:delta]
        new_lines = []
        for line in s_lines:
            if line.strip():
                new_lines.append(indent_addition + line)
            else:
                new_lines.append(line)
        comment.code_suggestion = "\n".join(new_lines)


def sanitize_code_suggestion(suggestion: str | None) -> str | None:
    """Sanitise code_suggestion by stripping outer markdown code block fences and line number prefixes."""
    if not suggestion:
        return None

    cleaned = suggestion.strip("\r\n")
    if not cleaned or not cleaned.strip():
        return None

    # Strip outer markdown code block fences if model enclosed suggestion in ```...```
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()

    if not cleaned:
        return None

    # Strip line number prefixes (e.g. '105 | ', '  105 + | ', '105 - | ', 'L105: ')
    # Plain numeric prefixes must be pipe-delimited (|) to avoid stripping dict keys (e.g., '105: "foo"').
    prefix_pattern = re.compile(r"^\s*(?:L\d+[\s\t]*[:|]|\d+[\s\t]*(?:[+-][\s\t]*)?\|)[\s\t]?")

    lines = cleaned.splitlines()
    if any(prefix_pattern.match(line) for line in lines):
        cleaned = "\n".join(prefix_pattern.sub("", line, count=1) for line in lines)

    return cleaned if cleaned.strip() else None


def filter_review_comments(review: ReviewResult, text_files: list) -> ReviewResult:
    """Filter inline comments to ensure they apply to valid lines in the diff,
    redirecting others to general feedback. Sanitises multi-line start_line bounds
    and code suggestions.
    """
    fn_get_valid_diff_lines = _get_pr_review_func("get_valid_diff_lines", get_valid_diff_lines)

    # Map file path -> tuple of valid line number sets (RIGHT, LEFT)
    file_patches = {f["filename"]: f.get("patch", "") for f in text_files}
    valid_lines_by_file = {filename: fn_get_valid_diff_lines(patch) for filename, patch in file_patches.items()}

    filtered_comments = []
    redirected_feedback = []

    for comment in review.comments:
        if comment.code_suggestion:
            comment.code_suggestion = sanitize_code_suggestion(comment.code_suggestion)

        comment_path = comment.path.replace("\\", "/")

        matched_file = None
        for fn in valid_lines_by_file:
            if fn.replace("\\", "/").lower() == comment_path.lower():
                matched_file = fn
                break

        if not matched_file:
            warning_msg = (
                f"Warning: Redirecting inline comment on {comment.path}:{comment.line} (File not found in PR changes)."
            )
            print(warning_msg, file=sys.stderr)

            feedback_item = f"**{comment.path}** (Line {comment.line}): {comment.severity} {comment.comment_text}"
            if comment.code_suggestion:
                feedback_item += f"\n  ```suggestion\n  {comment.code_suggestion}\n  ```"
            redirected_feedback.append(feedback_item)
            continue

        valid_right, valid_left = valid_lines_by_file[matched_file]
        is_left = comment.side and comment.side.upper() == "LEFT"
        valid_set = valid_left if is_left else valid_right

        # Auto-correct multi-line suggestion range bounds if start_line is omitted
        _auto_correct_suggestion_range(comment, matched_file, valid_set)

        # Auto-align code suggestion base indentation with target line in source file
        _auto_align_suggestion_indentation(comment, matched_file)

        # Validate start_line range if present
        if comment.start_line is not None:
            if comment.start_line > comment.line:
                # Swap inverted range bounds
                comment.start_line, comment.line = comment.line, comment.start_line
            elif comment.start_line == comment.line:
                comment.start_line = None

            if comment.start_line is not None and comment.start_line not in valid_set:
                comment.start_line = None

        if comment.line in valid_set:
            comment.path = matched_file
            filtered_comments.append(comment)
        else:
            warning_msg = (
                f"Warning: Redirecting inline comment on {comment.path}:{comment.line} (Line not in PR diff patch)."
            )
            print(warning_msg, file=sys.stderr)

            line_str = (
                f"Lines {comment.start_line}-{comment.line}"
                if comment.start_line is not None
                else f"Line {comment.line}"
            )
            feedback_item = f"**{comment.path}** ({line_str}): {comment.severity} {comment.comment_text}"
            if comment.code_suggestion:
                feedback_item += f"\n  ```suggestion\n  {comment.code_suggestion}\n  ```"
            redirected_feedback.append(feedback_item)

    if redirected_feedback:
        review.general_feedback.append("💡 **Additional Feedback on Unmodified Lines:**")
        review.general_feedback.extend(redirected_feedback)

    review.comments = filtered_comments
    return review


def get_file_content(path: str) -> str:
    """Read file content safely as UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def get_local_git_files() -> list:
    """Developer fallback to gather file diffs from local git tree."""
    try:
        res = subprocess.run(["git", "diff", "main...HEAD", "--name-only"], capture_output=True, text=True, check=True)
        filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]
        diff_base = "main...HEAD"

        if not filenames:
            # Fall back to uncommitted/staged working tree changes vs HEAD
            res = subprocess.run(["git", "diff", "HEAD", "--name-only"], capture_output=True, text=True, check=True)
            filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]
            diff_base = "HEAD"

        files = []
        for filename in filenames:
            diff_res = subprocess.run(
                ["git", "diff", diff_base, "--", filename], capture_output=True, text=True, check=True
            )
            files.append({"filename": filename, "status": "modified", "patch": diff_res.stdout})
        return files

    except Exception as e:
        print(f"Error running local git diff: {e}", file=sys.stderr)
        return []


def get_all_repo_files() -> list[str]:
    """Get list of all tracked text files in the repository."""
    fn_is_text_file = _get_pr_review_func("is_text_file", is_text_file)
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
        all_files = [f.strip() for f in res.stdout.split("\n") if f.strip()]
        return [f.replace("\\", "/") for f in all_files if fn_is_text_file(f) and os.path.exists(f)]
    except Exception as e:
        print(f"Error running git ls-files: {e}", file=sys.stderr)
        # Fallback to os.walk if git is not available
        text_files = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                filepath = os.path.relpath(os.path.join(root, file), ".")
                if fn_is_text_file(filepath) and os.path.exists(filepath):
                    text_files.append(filepath.replace("\\", "/"))
        return text_files


def is_core_file(filename: str, patterns: list[str]) -> bool:
    """Check if the filename matches any of the core file patterns (case-insensitive)."""
    norm_path = filename.replace("\\", "/").removeprefix("./")
    basename = os.path.basename(norm_path)
    posix_path = PurePosixPath(norm_path.lower())

    for pattern in patterns:
        norm_pat = pattern.replace("\\", "/").removeprefix("./").lower()
        if "/" in norm_pat:
            if posix_path.match(norm_pat):
                return True
        else:
            if (
                fnmatch.fnmatch(basename.lower(), norm_pat)
                or fnmatch.fnmatch(norm_path.lower(), norm_pat)
                or posix_path.match(norm_pat)
            ):
                return True
    return False


def generate_file_tree(files: list[str]) -> str:
    """Generate a text-based folder tree structure from a list of file paths."""
    tree = {}
    for f in sorted(files):
        parts = f.replace("\\", "/").split("/")
        curr = tree
        for part in parts:
            if part not in curr:
                curr[part] = {}
            curr = curr[part]

    def _render(node: dict, indent: str = "") -> list[str]:
        lines = []
        keys = list(node.keys())
        for idx, key in enumerate(keys):
            is_last = idx == len(keys) - 1
            marker = "└── " if is_last else "├── "
            child_indent = "    " if is_last else "│   "
            if node[key]:
                lines.append(f"{indent}{marker}{key}/")
                lines.extend(_render(node[key], indent + child_indent))
            else:
                lines.append(f"{indent}{marker}{key}")
        return lines

    return ".\n" + "\n".join(_render(tree))


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


def count_text_tokens(client, model_name: str, text: str) -> int:
    """Count or estimate the number of tokens in a text string."""
    if not text:
        return 0
    if client and hasattr(client, "models") and hasattr(client.models, "count_tokens"):
        try:
            resp = client.models.count_tokens(model=model_name, contents=text)
            if hasattr(resp, "total_tokens") and resp.total_tokens is not None:
                return resp.total_tokens
        except Exception:
            pass
    # Fallback heuristic (~4 chars per token)
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
