"""
Description: Resolving GitHub review threads the reviewer itself has declared addressed.

The problem this solves: the action already recognises when a prior finding has been fixed and
prints it under "Resolved Items from Prior Reviews", but it never touches the thread. With
"Require conversation resolution before merging" enabled, the merge stays blocked on a finding the
reviewer has already agreed is done, and a human has to click Resolve on the reviewer's behalf.

Three safety rules, because resolving a thread hides feedback and a wrong resolution is worse than
no resolution at all:

1. **Only threads authored by this action.** A human reviewer's thread is never touched, no matter
   what the model claims to have resolved. This is enforced against the thread's first comment
   author, not against the model's opinion.
2. **Only exact (path, line) matches.** The model supplies the location; nothing is inferred from
   prose. An item without a location is reported and left alone.
3. **Failure is non-fatal.** A review that is already posted must not be lost because a follow-up
   mutation failed, so every error here is a warning.

GraphQL only: `resolveReviewThread` has no REST equivalent. `GITHUB_TOKEN` with `pull-requests:
write` can call it, which the action already requires.
"""

import sys
from typing import Any

import requests

from gemini_review.config import DEFAULT_TIMEOUT

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

# Bot logins that represent "this action". A review posted with the default GITHUB_TOKEN is
# authored by github-actions[bot]; a PAT or GitHub App posts under its own name, which is why the
# expected author is passed in rather than hardcoded.
_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          viewerCanResolve
          comments(first: 1) {
            nodes {
              path
              line
              originalLine
              author { login }
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def _graphql(headers: dict, query: str, variables: dict, timeout: int) -> dict[str, Any] | None:
    """POST a GraphQL document, returning `data` or None. Never raises."""
    try:
        res = requests.post(
            GITHUB_GRAPHQL_URL,
            headers=headers,
            json={"query": query, "variables": variables},
            timeout=timeout,
        )
    except requests.RequestException as e:
        print(f"Warning: GitHub GraphQL request failed: {e}", file=sys.stderr)
        return None

    if res.status_code != 200:
        print(f"Warning: GitHub GraphQL returned {res.status_code}: {res.text[:300]}", file=sys.stderr)
        return None

    try:
        payload = res.json()
    except ValueError:
        print("Warning: GitHub GraphQL returned a non-JSON body.", file=sys.stderr)
        return None

    # GraphQL reports errors in a 200 response, so status alone is not success.
    if payload.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in payload["errors"])
        print(f"Warning: GitHub GraphQL error: {messages}", file=sys.stderr)
        return None

    return payload.get("data")


def fetch_review_threads(repository: str, pr_number: int, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Every review thread on the PR, following pagination. Empty list on any failure."""
    try:
        owner, name = repository.split("/", 1)
    except ValueError:
        print(f"Warning: cannot parse repository '{repository}'.", file=sys.stderr)
        return []

    threads: list[dict] = []
    cursor = None
    # Bounded rather than `while True`: a malformed pageInfo should not loop forever inside CI.
    for _ in range(20):
        data = _graphql(
            headers,
            _THREADS_QUERY,
            {"owner": owner, "name": name, "pr": pr_number, "cursor": cursor},
            timeout,
        )
        if not data:
            return threads
        try:
            block = data["repository"]["pullRequest"]["reviewThreads"]
        except (KeyError, TypeError):
            return threads
        threads.extend(block.get("nodes") or [])
        page = block.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
    return threads


def _thread_key(thread: dict) -> tuple[str, int] | None:
    """(path, line) for a thread, or None when it cannot be located.

    `originalLine` is preferred over `line`: `line` shifts as the file changes underneath the
    comment and becomes null once the thread is outdated, whereas `originalLine` still points at
    the position the finding was raised against. Matching on `line` would silently stop matching
    exactly when the developer edits the file, which is the moment the item gets resolved.
    """
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return None
    first = comments[0]
    path = first.get("path")
    line = first.get("originalLine")
    if line is None:
        line = first.get("line")
    if not path or line is None:
        return None
    return (path, int(line))


def _authored_by(thread: dict, expected_logins: set[str]) -> bool:
    comments = (thread.get("comments") or {}).get("nodes") or []
    if not comments:
        return False
    author = (comments[0].get("author") or {}).get("login") or ""
    return author.lower() in {login.lower() for login in expected_logins}


def resolve_addressed_threads(
    repository: str,
    pr_number: int,
    headers: dict,
    resolved_items: list,
    bot_logins: set[str],
    timeout: int = DEFAULT_TIMEOUT,
    dry_run: bool = False,
) -> list[str]:
    """Resolve threads matching `resolved_items`. Returns human-readable descriptions of what was resolved.

    Only unresolved threads authored by `bot_logins` and matching an item's exact (path, line) are
    touched. Everything else is left alone and explained on stderr, because a silent no-op here
    looks identical to a bug.
    """
    located = [(i.path, int(i.line)) for i in resolved_items if getattr(i, "path", None) and getattr(i, "line", None)]
    skipped = len(resolved_items) - len(located)
    if skipped:
        print(
            f"Thread resolution: {skipped} resolved item(s) had no file/line attribution and were left open.",
            file=sys.stderr,
        )
    if not located:
        return []

    threads = fetch_review_threads(repository, pr_number, headers, timeout)
    if not threads:
        print("Thread resolution: no review threads returned; nothing to resolve.", file=sys.stderr)
        return []

    wanted = set(located)
    resolved: list[str] = []
    for thread in threads:
        if thread.get("isResolved"):
            continue
        key = _thread_key(thread)
        if key is None or key not in wanted:
            continue
        if not _authored_by(thread, bot_logins):
            # A human raised this. The model does not get to close it.
            print(
                f"Thread resolution: skipping {key[0]}:{key[1]} — the thread was not opened by this reviewer.",
                file=sys.stderr,
            )
            continue
        if not thread.get("viewerCanResolve"):
            print(
                f"Thread resolution: skipping {key[0]}:{key[1]} — the token cannot resolve it "
                "(needs pull-requests: write).",
                file=sys.stderr,
            )
            continue
        if dry_run:
            print(f"Thread resolution [dry run]: would resolve {key[0]}:{key[1]}.", file=sys.stderr)
            resolved.append(f"{key[0]}:{key[1]}")
            continue

        data = _graphql(headers, _RESOLVE_MUTATION, {"threadId": thread["id"]}, timeout)
        if data and (data.get("resolveReviewThread") or {}).get("thread", {}).get("isResolved"):
            resolved.append(f"{key[0]}:{key[1]}")
        else:
            print(f"Warning: failed to resolve thread at {key[0]}:{key[1]}.", file=sys.stderr)

    if resolved:
        print(f"Thread resolution: resolved {len(resolved)} thread(s): {', '.join(resolved)}", file=sys.stderr)
    return resolved


def reviewer_logins() -> set[str]:
    """Logins that count as "this action" when deciding whether a thread is ours to resolve.

    With the default GITHUB_TOKEN the review is authored by `github-actions[bot]`. A PAT or a
    GitHub App posts under its own name, so GEMINI_REVIEWER_LOGIN allows that to be declared
    rather than guessed. Guessing wrong fails safe: no thread matches, nothing is resolved.
    """
    import os

    logins = {"github-actions[bot]", "github-actions"}
    extra = os.environ.get("GEMINI_REVIEWER_LOGIN", "").strip()
    if extra:
        logins.update(part.strip() for part in extra.split(",") if part.strip())
    return logins
