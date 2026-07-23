"""
Description: General file, git, diff, and text manipulation utilities.
Provides functions for filtering binary files, parsing diff patches,
generating repo file trees, and loading workspace agent rules.
"""

import fnmatch
import os
import subprocess
import sys
from typing import Any

from gemini_review.schemas import ReviewResult


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


def get_valid_changed_lines(patch: str) -> set[int]:
    """Parse the diff patch to find all line numbers in the new file (RIGHT side) that are part of the diff."""
    valid_lines = set()
    if not patch:
        return valid_lines

    current_line = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            try:
                # Header format: @@ -old_start,old_count +new_start,new_count @@
                parts = line.split()
                new_info = parts[2].lstrip("+")
                if "," in new_info:
                    start_line, _ = new_info.split(",")
                else:
                    start_line = new_info
                current_line = int(start_line)
            except Exception:
                current_line = 0
        elif line.startswith("+") or line.startswith(" ") or line == "":
            if current_line > 0:
                valid_lines.add(current_line)
                current_line += 1
        elif line.startswith("-"):
            # Deleted lines do not advance line numbers in the new file (RIGHT side)
            pass
    return valid_lines


def filter_review_comments(review: ReviewResult, text_files: list) -> ReviewResult:
    """Filter inline comments to ensure they apply to valid lines in the diff,
    redirecting others to general feedback.
    """
    fn_get_valid_changed_lines = _get_pr_review_func("get_valid_changed_lines", get_valid_changed_lines)
    # Map file path -> set of valid line numbers
    file_patches = {f["filename"]: f.get("patch", "") for f in text_files}
    valid_lines_by_file = {filename: fn_get_valid_changed_lines(patch) for filename, patch in file_patches.items()}

    filtered_comments = []
    redirected_feedback = []

    for comment in review.comments:
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

        valid_lines = valid_lines_by_file[matched_file]
        if comment.line in valid_lines:
            comment.path = matched_file
            filtered_comments.append(comment)
        else:
            warning_msg = (
                f"Warning: Redirecting inline comment on {comment.path}:{comment.line} (Line not in PR diff patch)."
            )
            print(warning_msg, file=sys.stderr)

            feedback_item = f"**{comment.path}** (Line {comment.line}): {comment.severity} {comment.comment_text}"
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

        files = []
        for filename in filenames:
            diff_res = subprocess.run(
                ["git", "diff", "main...HEAD", "--", filename], capture_output=True, text=True, check=True
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
    """Check if the filename matches any of the core file patterns."""
    basename = os.path.basename(filename)
    for pattern in patterns:
        if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(filename, pattern):
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
