"""
Description: GitHub REST API integration helper module.
Handles fetching changed files, retrieving prior inline review & issue conversation comments,
and posting review summaries and inline comments.
"""

import os
import sys
import time
from typing import Any

import requests

from gemini_review.config import DEFAULT_TIMEOUT
from gemini_review.pricing import estimate_cost, usd
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


def parse_excluded_authors(raw: str | None) -> set[str]:
    """Logins whose comments must not enter the prompt. Case-insensitive, comma-separated."""
    if not raw:
        return set()
    return {part.strip().lower() for part in str(raw).split(",") if part.strip()}


def _author_login(comment: dict) -> str:
    return ((comment or {}).get("user") or {}).get("login") or ""


def filter_comment_authors(comments: list[dict], excluded: set[str]) -> list[dict]:
    """Drop comments written by an excluded author, keeping everything else untouched."""
    if not excluded:
        return comments
    return [c for c in comments if isinstance(c, dict) and _author_login(c).lower() not in excluded]


def format_pr_comment_history(
    review_comments: list[dict],
    issue_comments: list[dict],
    exclude_authors: set[str] | None = None,
) -> str:
    """Format inline review comments into structured threads, and general issue comments into conversation history.

    `exclude_authors` drops comments by the given logins before formatting, so excluded content
    never reaches the prompt and never appears in the reported token count.
    """
    if exclude_authors:
        review_comments = filter_comment_authors(review_comments, exclude_authors)
        issue_comments = filter_comment_authors(issue_comments, exclude_authors)

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


# Status codes worth trying again. 5xx and 429 are GitHub saying "not now" rather than "no": the
# request was well-formed and would have succeeded a moment earlier or later. 4xx others are not
# retried, because a 403 or 422 will fail identically however many times it is sent.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Two retries, ~1s then ~3s. Deliberately small: a review that arrives four minutes late is not
# much use on a PR someone is waiting to merge, and the job failing loudly is an acceptable
# outcome. This is for the transient blip, not for riding out an incident.
POST_RETRIES = 2
RETRY_BACKOFF_SECONDS = (1, 3)


def _format_resolved_item(item: Any) -> str:
    """Render one resolved item. Accepts the structured form or a bare string.

    The string branch exists because a cached or replayed response from before `resolved_items`
    became structured would otherwise crash the render of an otherwise valid review.
    """
    if isinstance(item, str):
        return item
    text = getattr(item, "description", None) or str(item)
    path = getattr(item, "path", None)
    line = getattr(item, "line", None)
    if path and line:
        return f"{text} (`{path}:{line}`)"
    if path:
        return f"{text} (`{path}`)"
    return text


def post_with_retry(url: str, headers: dict, json_payload: dict, timeout: int) -> Any:
    """POST, retrying only on transient GitHub failures.

    Why this exists: during a GitHub incident the reviewer computed three complete reviews, paid for
    the tokens, and lost all of them to 503s on the way back. The work was done and thrown away. One
    retry would have delivered most of it.
    """
    last_error: Exception | None = None
    for attempt in range(POST_RETRIES + 1):
        try:
            response = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
            if response.status_code not in RETRYABLE_STATUS or attempt == POST_RETRIES:
                return response
            status_desc = f"{response.status_code}"
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt == POST_RETRIES:
                raise
            status_desc = f"network error ({e})"

        delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
        print(
            f"Notice: GitHub returned {status_desc}, retrying in {delay}s ({attempt + 1}/{POST_RETRIES})...",
            file=sys.stderr,
        )
        time.sleep(delay)

    if last_error:
        raise last_error
    return response


def post_review(
    repository: str,
    pr_number: int,
    commit_id: str,
    review: ReviewResult,
    headers: dict,
    timeout: int = DEFAULT_TIMEOUT,
    usage_metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    event_name: str = "pull_request",
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
        resolved_str = "\n".join(f"- {_format_resolved_item(r)}" for r in review.resolved_items)
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

        # Cost, from the same counts. A model with no rate entry renders tokens only —
        # borrowing another model's rate would produce a confident wrong figure.
        cost = estimate_cost(usage_metadata, usage_metadata.get("model"), config)
        cost_rows = []
        if cost.rate:
            cost_rows = [
                f"| **Cost (uncached input)** | {usd(cost.uncached_input)} |",
            ]
            if cached_tokens > 0:
                cost_rows.append(f"| **Cost (cached input)** | {usd(cost.cached_input)} |")
            cost_rows.extend(
                [
                    f"| **Cost (output)** | {usd(cost.output)} |",
                    f"| **Estimated Total Cost** | **{usd(cost.total)}** |",
                ]
            )

        caveat_md = ""
        if cost.caveats:
            caveat_md = "\n\n" + "\n".join(f"> {c}" for c in cost.caveats)

        telemetry_md = (
            "<details>\n"
            "<summary>📊 Token Usage & Cost Efficiency</summary>\n\n"
            "| Metric | Value |\n"
            "| :--- | :---: |\n" + "\n".join(table_rows + cost_rows) + caveat_md + "\n\n</details>"
        )
        body_sections.append(telemetry_md)

    review_body = "\n\n".join(body_sections)

    payload = {"body": review_body, "event": "COMMENT", "comments": comments_payload}

    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews"
    print(f"Submitting review to PR #{pr_number} on {repository}...", file=sys.stderr)
    res = post_with_retry(url, headers, payload, timeout)

    if res.status_code in (200, 201):
        print("Successfully posted PR review atomically.", file=sys.stderr)
        if event_name != "pull_request":
            post_commit_status(
                repository, commit_id, "success", "Gemini PR Review completed successfully", headers, timeout=timeout
            )
        return

    print(f"Warning: Failed to submit review atomically (status {res.status_code}). Error: {res.text}", file=sys.stderr)
    print("Falling back to posting summary and comments individually...", file=sys.stderr)

    posted_anything = False

    # 1. Post review summary as a single comment on the PR conversation
    issue_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    res_summary = post_with_retry(issue_url, headers, {"body": review_body}, timeout)
    if res_summary.status_code in (200, 201):
        posted_anything = True
    else:
        print(f"Error posting review summary comment: {res_summary.status_code} - {res_summary.text}", file=sys.stderr)

    # 2. Post inline comments one by one
    comments_url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/comments"
    for idx, c in enumerate(comments_payload):
        c_payload = {"body": c["body"], "commit_id": commit_id, "path": c["path"], "line": c["line"], "side": c["side"]}
        if "start_line" in c:
            c_payload["start_line"] = c["start_line"]
            c_payload["start_side"] = c["start_side"]
        res_comment = post_with_retry(comments_url, headers, c_payload, timeout)
        if res_comment.status_code in (200, 201):
            posted_anything = True
            print(f"Posted comment {idx + 1}/{len(comments_payload)} successfully.", file=sys.stderr)
        else:
            print(
                f"Error posting comment {idx + 1} on {c['path']} (line {c['line']}): {res_comment.status_code} -"
                f" {res_comment.text}",
                file=sys.stderr,
            )

    if not posted_anything:
        if event_name != "pull_request":
            post_commit_status(
                repository,
                commit_id,
                "failure",
                "Gemini PR Review failed: Unable to post review comments to GitHub",
                headers,
                timeout=timeout,
            )
        raise RuntimeError(
            f"Failed to post PR review to GitHub (atomic status: {res.status_code}, "
            f"summary status: {res_summary.status_code})."
        )

    if event_name != "pull_request":
        post_commit_status(
            repository, commit_id, "success", "Gemini PR Review completed successfully", headers, timeout=timeout
        )


def post_commit_status(
    repository: str,
    commit_id: str,
    state: str,
    description: str,
    headers: dict,
    context: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    """Post a commit status update to GitHub's Statuses API to update PR check marks."""
    if not repository or not commit_id or commit_id == "mock_head_sha":
        return

    if context is None:
        workflow_name = os.environ.get("GITHUB_WORKFLOW", "Gemini Code Review")
        context = f"{workflow_name} / review"

    url = f"https://api.github.com/repos/{repository}/statuses/{commit_id}"
    payload = {
        "state": state,
        "description": description[:140],
        "context": context,
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if res.status_code in (200, 201):
            print(f"Updated GitHub commit status '{context}' to '{state}'.", file=sys.stderr)
        else:
            print(
                f"Warning: Failed to update GitHub commit status ({res.status_code}): {res.text}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Warning: Exception updating GitHub commit status: {e}", file=sys.stderr)


def is_inline_suggestion_commit(
    repository: str, commit_sha: str, headers: dict, timeout: int = DEFAULT_TIMEOUT
) -> bool:
    """Check whether a given commit SHA was created by accepting an inline suggestion via GitHub UI."""
    if not repository or not commit_sha or commit_sha == "mock_head_sha":
        return False

    url = f"https://api.github.com/repos/{repository}/commits/{commit_sha}"
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code != 200:
            return False
        data = res.json()

        committer = data.get("committer") or {}
        committer_login = committer.get("login", "")
        commit_info = data.get("commit") or {}
        committer_email = (commit_info.get("committer") or {}).get("email", "")
        commit_msg = commit_info.get("message", "")

        is_web_flow = committer_login == "web-flow" or committer_email == "noreply@github.com"
        has_bot_coauthor = "github-actions[bot]" in commit_msg
        is_suggestion_msg = (
            commit_msg.startswith("Update ")
            or commit_msg.startswith("Apply suggestion")
            or commit_msg.startswith("Apply suggestions")
        )

        return is_web_flow and (has_bot_coauthor or is_suggestion_msg)
    except Exception as e:
        print(f"Warning: Failed to check commit details for inline suggestion detection: {e}", file=sys.stderr)
        return False
