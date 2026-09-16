"""Diff patch parsing, line numbering, and review comment filtering utilities.

This module provides tools for annotating unified diff patches, verifying line numbers,
sanitising code suggestions, aligning indentation, and filtering inline comments.
"""

import re
import sys

from gemini_review.schemas import InlineComment, ReviewResult


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
    from gemini_review.repo import get_file_content
    from gemini_review.utils import _get_pr_review_func

    if not comment.code_suggestion:
        return

    s_lines = [sl.strip() for sl in comment.code_suggestion.strip().splitlines() if sl.strip()]
    if len(s_lines) <= 1:
        return

    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    content = fn_get_file_content(matched_file)
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
    from gemini_review.repo import get_file_content
    from gemini_review.utils import _get_pr_review_func

    if not comment.code_suggestion:
        return

    fn_get_file_content = _get_pr_review_func("get_file_content", get_file_content)
    content = fn_get_file_content(matched_file)
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
    from gemini_review.utils import _get_pr_review_func

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
