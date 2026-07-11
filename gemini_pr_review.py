# /// script
# dependencies = [
#   "google-genai>=2.10.0",
#   "requests",
#   "pydantic",
# ]
# ///
#!/usr/bin/env python3
"""
Description: Runs a Pull Request code review using the Google GenAI SDK.
Supports both standard PR events and comment-triggered '/gemini-review' runs.
Includes dry-run mode for local developers to test and run offline.
"""

import json
import os
import sys
import tomllib

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class InlineComment(BaseModel):
    path: str = Field(description="The relative file path being reviewed.")
    line: int = Field(description="The line number in the RIGHT (new/modified) version of the file where the comment applies.")
    side: str = Field(default="RIGHT", description="Must be 'RIGHT' for additions/modifications or 'LEFT' for deletions.")
    severity: str = Field(description="Severity icon: 🔴 (Critical), 🟠 (High), 🟡 (Medium), 🟢 (Low)")
    comment_text: str = Field(description="Constructive feedback explaining the issue. Write the feedback comments using English (UK) spelling, but do not flag US spelling in the codebase unless it is a genuine typo.")
    code_suggestion: str | None = Field(None, description="Optional drop-in code suggestion replacement. Must match the exact structure and indentation of the replaced code, formatted as a suggestion.")


class ReviewResult(BaseModel):
    summary: str = Field(description="A brief, high-level assessment of the Pull Request's objective and quality (2-3 sentences).")
    general_feedback: list[str] = Field(description="A list of general observations, positive highlights, or recurring patterns.")
    comments: list[InlineComment] = Field(description="List of targeted inline comments on the code changes.")


def is_text_file(filename: str) -> bool:
    """Filter out typical binary, lock, and encrypted file formats."""
    excluded_extensions = {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".zip", ".tar", ".gz",
        ".enc", ".lock", ".db", ".pyc", ".o", ".so", ".dylib", ".dll", ".exe",
        ".woff", ".woff2", ".eot", ".ttf"
    }
    _, ext = os.path.splitext(filename.lower())
    if ext in excluded_extensions:
        return False

    excluded_names = {"package-lock.json", "uv.lock", ".env", ".env.enc", ".envrc"}
    if os.path.basename(filename) in excluded_names:
        return False

    return True


def get_file_content(path: str) -> str:
    """Read file content safely as UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def get_pr_files(repository: str, pr_number: int, headers: dict) -> list:
    """Fetch changed files list in PR using pagination."""
    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files?page={page}&per_page=100"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Error fetching files: {response.status_code} - {response.text}", file=sys.stderr)
            break
        data = response.json()
        if not data:
            break
        files.extend(data)
        page += 1
    return files


def get_local_git_files() -> list:
    """Developer fallback to gather file diffs from local git tree."""
    import subprocess
    try:
        res = subprocess.run(["git", "diff", "main...HEAD", "--name-only"], capture_output=True, text=True, check=True)
        filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]

        files = []
        for filename in filenames:
            diff_res = subprocess.run(["git", "diff", "main...HEAD", "--", filename], capture_output=True, text=True, check=True)
            files.append({
                "filename": filename,
                "status": "modified",
                "patch": diff_res.stdout
            })
        return files
    except Exception as e:
        print(f"Error running local git diff: {e}", file=sys.stderr)
        return []


def load_system_instruction(repository: str | None, pr_number: int) -> str:
    """Load system instructions from Dazbo's gemini-review.toml prompt configuration."""
    path = ".github/commands/gemini-review.toml"
    if not os.path.exists(path):
        return "You are a world-class code review agent. Analyze changes and output constructive feedback using English (UK) spelling."

    with open(path, "rb") as f:
        config = tomllib.load(f)

    prompt = config.get("prompt", "")
    prompt = prompt.replace("!{echo $REPOSITORY}", repository or "unknown")
    prompt = prompt.replace("!{echo $PULL_REQUEST_NUMBER}", str(pr_number))
    prompt = prompt.replace("!{echo $ADDITIONAL_CONTEXT}", "")
    return prompt


def build_prompt(files: list) -> str:
    """Consolidate file patches and file contents into a single review context."""
    prompt_parts = []
    prompt_parts.append("Below are the files and changes included in this Pull Request:\n")

    for f in files:
        filename = f["filename"]
        status = f["status"]
        patch = f.get("patch", "")

        if not is_text_file(filename) or not patch:
            continue

        full_content = get_file_content(filename)

        prompt_parts.append(f"=== File: {filename} ===")
        prompt_parts.append(f"Status: {status}")
        prompt_parts.append("--- Diff (Patch) ---")
        prompt_parts.append(patch)
        if full_content:
            prompt_parts.append("--- Full Current File Content ---")
            prompt_parts.append(full_content)
        prompt_parts.append("=========================\n")

    return "\n".join(prompt_parts)


def post_review(repository: str, pr_number: int, commit_id: str, review: ReviewResult, headers: dict) -> None:
    """Submit review comments atomically or fall back to individual comments if needed."""
    comments_payload = []
    for c in review.comments:
        body_parts = [f"{c.severity} {c.comment_text}"]
        if c.code_suggestion:
            body_parts.append(f"```suggestion\n{c.code_suggestion}\n```")

        comments_payload.append({
            "path": c.path,
            "line": c.line,
            "side": c.side,
            "body": "\n\n".join(body_parts)
        })

    review_body = f"## 📋 Review Summary\n\n{review.summary}\n\n## 🔍 General Feedback\n\n" + "\n".join(f"- {f}" for f in review.general_feedback)

    payload = {
        "body": review_body,
        "event": "COMMENT",
        "comments": comments_payload
    }

    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews"
    print(f"Submitting review to PR #{pr_number} on {repository}...", file=sys.stderr)
    res = requests.post(url, headers=headers, json=payload)

    if res.status_code in (200, 201):
        print("Successfully posted PR review atomically.", file=sys.stderr)
        return

    print(f"Warning: Failed to submit review atomically (status {res.status_code}). Error: {res.text}", file=sys.stderr)
    print("Falling back to posting summary and comments individually...", file=sys.stderr)

    # 1. Post review summary as a single comment on the PR conversation
    issue_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    res_summary = requests.post(issue_url, headers=headers, json={"body": review_body})
    if res_summary.status_code not in (200, 201):
        print(f"Error posting review summary comment: {res_summary.status_code} - {res_summary.text}", file=sys.stderr)

    # 2. Post inline comments one by one
    comments_url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/comments"
    for idx, c in enumerate(comments_payload):
        c_payload = {
            "body": c["body"],
            "commit_id": commit_id,
            "path": c["path"],
            "line": c["line"],
            "side": c["side"]
        }
        res_comment = requests.post(comments_url, headers=headers, json=c_payload)
        if res_comment.status_code in (200, 201):
            print(f"Posted comment {idx+1}/{len(comments_payload)} successfully.", file=sys.stderr)
        else:
            print(f"Error posting comment {idx+1} on {c['path']} (line {c['line']}): {res_comment.status_code} - {res_comment.text}", file=sys.stderr)


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    use_vertexai = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("true", "1")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = os.environ.get("GEMINI_MODEL", os.environ.get("MODEL", "gemini-3.5-flash"))

    headers = {}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    headers["Accept"] = "application/vnd.github.v3+json"

    is_dry_run = False
    pr_number = 1
    head_sha = "mock_head_sha"

    if not event_path or not os.path.exists(event_path):
        print("Warning: GITHUB_EVENT_PATH not set or not found. Running in dry-run/mock mode.", file=sys.stderr)
        is_dry_run = True
    else:
        with open(event_path, encoding="utf-8") as f:
            event_payload = json.load(f)
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")

        if event_name == "pull_request":
            pr_number = event_payload["pull_request"]["number"]
            head_sha = event_payload["pull_request"]["head"]["sha"]
        elif event_name == "issue_comment":
            comment_body = event_payload["comment"]["body"].strip()
            if not comment_body.startswith("/gemini-review"):
                print("Comment does not start with /gemini-review. Exiting gracefully.", file=sys.stderr)
                sys.exit(0)

            author_association = event_payload["comment"]["author_association"]
            allowed_associations = {"OWNER", "MEMBER", "COLLABORATOR"}
            if author_association not in allowed_associations:
                print(f"User association '{author_association}' not authorized to trigger code review. Exiting.", file=sys.stderr)
                sys.exit(0)

            if "pull_request" not in event_payload["issue"]:
                print("Comment is not on a pull request. Exiting.", file=sys.stderr)
                sys.exit(0)

            pr_number = event_payload["issue"]["number"]
            url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"
            res = requests.get(url, headers=headers)
            if res.status_code != 200:
                print(f"Error fetching PR details: {res.status_code} - {res.text}", file=sys.stderr)
                sys.exit(1)
            pr_data = res.json()
            head_sha = pr_data["head"]["sha"]
        else:
            print(f"Unsupported event type: {event_name}. Running in dry-run mode.", file=sys.stderr)
            is_dry_run = True

    # Gather file patches and full contents
    if is_dry_run:
        print("Gathering files from local git tree...", file=sys.stderr)
        files = get_local_git_files()
    else:
        print(f"Fetching files for PR #{pr_number} from GitHub API...", file=sys.stderr)
        files = get_pr_files(repository, pr_number, headers)

    if not files:
        print("No files modified in this PR. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Filter out excluded file types
    text_files = [f for f in files if is_text_file(f["filename"])]
    if not text_files:
        print("No text-based files to review. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Initialize Gemini client
    print(f"Initializing GenAI Client (Model: {model_name})...", file=sys.stderr)
    if use_vertexai:
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        client = genai.Client(api_key=gemini_api_key)

    system_instruction = load_system_instruction(repository, pr_number)
    prompt_context = build_prompt(text_files)

    print("Generating code review...", file=sys.stderr)
    response = client.models.generate_content(
        model=model_name,
        contents=prompt_context,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        )
    )

    review_data = json.loads(response.text)
    review = ReviewResult(**review_data)

    if is_dry_run:
        print("\n=== DRY RUN REVIEW SUMMARY ===", file=sys.stderr)
        print(f"Summary: {review.summary}")
        print("\n=== GENERAL FEEDBACK ===", file=sys.stderr)
        for gf in review.general_feedback:
            print(f"- {gf}")
        print("\n=== INLINE COMMENTS ===", file=sys.stderr)
        for c in review.comments:
            suggestion_str = f"\nSuggestion:\n{c.code_suggestion}" if c.code_suggestion else ""
            print(f"File: {c.path}:{c.line} ({c.side}) - Severity: {c.severity}\n{c.comment_text}{suggestion_str}\n")
    else:
        post_review(repository, pr_number, head_sha, review, headers)


if __name__ == "__main__":
    main()
