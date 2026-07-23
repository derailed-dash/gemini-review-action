"""
Description: GitHub REST API integration helper module.
Handles fetching changed files, retrieving prior inline review & issue conversation comments,
and posting review summaries and inline comments.
"""

import sys
from typing import Any

import requests

from gemini_review.config import DEFAULT_TIMEOUT
from gemini_review.schemas import ReviewResult


def get_pr_files(repository: str, pr_number: int, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> list:
    """Fetch changed files list in PR using pagination."""
    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files?page={page}&per_page=100"
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            print(f"Error fetching files: {response.status_code} - {response.text}", file=sys.stderr)
            break
        data = response.json()
        if not data:
            break
        files.extend(data)
        page += 1
    return files


def get_pr_comments(
    repository: str, pr_number: int, headers: dict, timeout: int = DEFAULT_TIMEOUT
) -> tuple[list[dict], list[dict]]:
    """Fetch inline review comments and general PR issue comments for a pull request using pagination."""
    review_comments = []
    issue_comments = []

    if not repository or not pr_number:
        return review_comments, issue_comments

    # 1. Fetch inline review comments with pagination
    page = 1
    while True:
        review_url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/comments?page={page}&per_page=100"
        try:
            res = requests.get(review_url, headers=headers, timeout=timeout)
            if res.status_code != 200:
                print(
                    f"Warning: Failed to fetch PR review comments ({res.status_code}): {res.text}",
                    file=sys.stderr,
                )
                break
            data = res.json()
            if not data or not isinstance(data, list):
                break
            review_comments.extend(data)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Warning: Exception while fetching PR review comments: {e}", file=sys.stderr)
            break

    # 2. Fetch general issue/PR timeline comments with pagination
    page = 1
    while True:
        issue_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments?page={page}&per_page=100"
        try:
            res = requests.get(issue_url, headers=headers, timeout=timeout)
            if res.status_code != 200:
                print(
                    f"Warning: Failed to fetch PR issue comments ({res.status_code}): {res.text}",
                    file=sys.stderr,
                )
                break
            data = res.json()
            if not data or not isinstance(data, list):
                break
            issue_comments.extend(data)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Warning: Exception while fetching PR issue comments: {e}", file=sys.stderr)
            break

    return review_comments, issue_comments


def format_pr_comment_history(review_comments: list[dict], issue_comments: list[dict]) -> str:
    """Format inline review comments into structured threads, and general issue comments into conversation history."""
    if not review_comments and not issue_comments:
        return ""

    prompt_parts = []
    prompt_parts.append("=== Prior PR Discussion & Review Threads ===")
    prompt_parts.append(
        "Below are previous comments and review discussion threads from this Pull Request. "
        "Review them to understand prior feedback and avoid repeating suggestions that have "
        "already been addressed, resolved, or disagreed with:\n"
    )

    if review_comments:
        prompt_parts.append("--- Inline Review Comment Threads ---")
        comments_by_id = {c["id"]: c for c in review_comments if isinstance(c, dict) and "id" in c}

        roots = []
        replies_by_root = {}
        for c in review_comments:
            if not isinstance(c, dict):
                continue
            reply_to = c.get("in_reply_to_id")
            if reply_to and reply_to in comments_by_id:
                curr = reply_to
                visited = set()
                while curr in comments_by_id and comments_by_id[curr].get("in_reply_to_id") and curr not in visited:
                    visited.add(curr)
                    curr = comments_by_id[curr]["in_reply_to_id"]
                root_id = curr
                if root_id in comments_by_id:
                    replies_by_root.setdefault(root_id, []).append(c)
                else:
                    roots.append(c)
            else:
                roots.append(c)

        for root in roots:
            root_id = root.get("id")
            file_path = root.get("path", "unknown")
            line = root.get("line") or root.get("original_line") or "N/A"
            author = root.get("user", {}).get("login", "unknown") if isinstance(root.get("user"), dict) else "unknown"
            body = root.get("body", "").strip()

            prompt_parts.append(f"• Thread on `{file_path}` (Line {line}):")
            prompt_parts.append(f"  - [{author}]: {body}")

            thread_replies = replies_by_root.get(root_id, [])
            for reply in thread_replies:
                r_author = (
                    reply.get("user", {}).get("login", "unknown") if isinstance(reply.get("user"), dict) else "unknown"
                )
                r_body = reply.get("body", "").strip()
                prompt_parts.append(f"    └─ [{r_author}]: {r_body}")
            prompt_parts.append("")

    if issue_comments:
        prompt_parts.append("--- General PR Conversation Comments ---")
        for comment in issue_comments:
            if not isinstance(comment, dict):
                continue
            author = (
                comment.get("user", {}).get("login", "unknown") if isinstance(comment.get("user"), dict) else "unknown"
            )
            body = comment.get("body", "").strip()
            created_at = comment.get("created_at", "")
            date_str = f" ({created_at[:10]})" if len(created_at) >= 10 else ""
            prompt_parts.append(f"• [{author}]{date_str}: {body}")
        prompt_parts.append("")

    prompt_parts.append("===========================================\n")
    return "\n".join(prompt_parts)


def post_review(
    repository: str,
    pr_number: int,
    commit_id: str,
    review: ReviewResult,
    headers: dict,
    timeout: int = DEFAULT_TIMEOUT,
    usage_metadata: dict[str, Any] | None = None,
) -> None:
    """Submit review comments atomically or fall back to individual comments if needed."""
    comments_payload = []
    for c in review.comments:
        body_parts = [f"{c.severity} {c.comment_text}"]
        if c.code_suggestion:
            body_parts.append(f"```suggestion\n{c.code_suggestion}\n```")

        comment_dict = {"path": c.path, "line": c.line, "side": c.side, "body": "\n\n".join(body_parts)}
        if getattr(c, "start_line", None) and c.start_line < c.line:
            comment_dict["start_line"] = c.start_line
            comment_dict["start_side"] = c.side

        comments_payload.append(comment_dict)

    body_sections = [f"## 📋 Review Summary\n\n{review.summary}"]
    if review.resolved_items:
        resolved_str = "\n".join(f"- {r}" for r in review.resolved_items)
        body_sections.append(f"### ✅ Resolved Items from Prior Reviews\n\n{resolved_str}")
    if review.general_feedback:
        feedback_str = "\n".join(f"- {f}" for f in review.general_feedback)
        body_sections.append(f"## 🔍 General Feedback\n\n{feedback_str}")

    if usage_metadata:
        cached_tokens = usage_metadata.get("cached_tokens", 0)
        fresh_tokens = usage_metadata.get("fresh_tokens", 0)
        comment_history_tokens = usage_metadata.get("comment_history_tokens", 0)
        candidates_tokens = usage_metadata.get("candidates_tokens", 0)
        total_tokens = usage_metadata.get("total_tokens", 0)
        cache_percentage = usage_metadata.get("cache_percentage", 0.0)

        cache_str = f" (⚡ {cache_percentage:.1f}% cached)" if cached_tokens > 0 else ""

        table_rows = [
            f"| **Input Tokens (uncached)** | {fresh_tokens:,d} |",
        ]
        if cached_tokens > 0:
            table_rows.append(f"| **Input Tokens (cached)** | {cached_tokens:,d}{cache_str} |")
        if comment_history_tokens > 0:
            table_rows.append(f"| **PR Comments History Tokens** | {comment_history_tokens:,d} |")
        table_rows.extend(
            [
                f"| **Output Tokens** | {candidates_tokens:,d} |",
                f"| **Total Session Tokens** | **{total_tokens:,d}** |",
            ]
        )

        telemetry_md = (
            "<details>\n"
            "<summary>📊 Token Usage & Cost Efficiency</summary>\n\n"
            "| Metric | Token Count |\n"
            "| :--- | :---: |\n" + "\n".join(table_rows) + "\n\n</details>"
        )
        body_sections.append(telemetry_md)

    review_body = "\n\n".join(body_sections)

    payload = {"body": review_body, "event": "COMMENT", "comments": comments_payload}

    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews"
    print(f"Submitting review to PR #{pr_number} on {repository}...", file=sys.stderr)
    res = requests.post(url, headers=headers, json=payload, timeout=timeout)

    if res.status_code in (200, 201):
        print("Successfully posted PR review atomically.", file=sys.stderr)
        return

    print(f"Warning: Failed to submit review atomically (status {res.status_code}). Error: {res.text}", file=sys.stderr)
    print("Falling back to posting summary and comments individually...", file=sys.stderr)

    # 1. Post review summary as a single comment on the PR conversation
    issue_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    res_summary = requests.post(issue_url, headers=headers, json={"body": review_body}, timeout=timeout)
    if res_summary.status_code not in (200, 201):
        print(f"Error posting review summary comment: {res_summary.status_code} - {res_summary.text}", file=sys.stderr)

    # 2. Post inline comments one by one
    comments_url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/comments"
    for idx, c in enumerate(comments_payload):
        c_payload = {"body": c["body"], "commit_id": commit_id, "path": c["path"], "line": c["line"], "side": c["side"]}
        if "start_line" in c:
            c_payload["start_line"] = c["start_line"]
            c_payload["start_side"] = c["start_side"]
        res_comment = requests.post(comments_url, headers=headers, json=c_payload, timeout=timeout)
        if res_comment.status_code in (200, 201):
            print(f"Posted comment {idx + 1}/{len(comments_payload)} successfully.", file=sys.stderr)
        else:
            print(
                f"Error posting comment {idx + 1} on {c['path']} (line {c['line']}): {res_comment.status_code} -"
                f" {res_comment.text}",
                file=sys.stderr,
            )
