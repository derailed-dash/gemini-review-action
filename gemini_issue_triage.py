#!/usr/bin/env python3
"""
Description: Runs an automated GitHub Issue triage using the Google GenAI SDK.
Loads prompt configuration from .github/commands/gemini-triage.toml, fetches
available labels from GitHub API, queries Gemini using Structured Outputs,
and applies the selected labels to the issue.

Outputs and logs (including errors and progress messages) are printed to stderr
and stdout, which are viewable in the GitHub Actions runner execution logs
for the workflow run.
"""

import json
import os
import sys
import tomllib
from typing import Any

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from gemini_review import (
    build_thinking_config,
    build_triage_multimodal_parts,
    extract_remote_image_urls,
    extract_response_text_or_raise,
    fetch_remote_image,
    filter_comment_authors,
    get_default_model,
    parse_excluded_authors,
)

DEFAULT_TIMEOUT = 60

__all__ = [
    "TriageResult",
    "get_available_labels",
    "get_issue_comments",
    "load_triage_prompt",
    "apply_labels",
    "extract_remote_image_urls",
    "fetch_remote_image",
    "build_triage_multimodal_parts",
    "main",
]


class TriageResult(BaseModel):
    """Represents the structured issue triage results returned by the Gemini model."""

    selected_labels: list[str] = Field(
        description=(
            "List of appropriate labels selected from the available labels list. Must match available labels exactly."
        )
    )
    reasoning: str = Field(description="A brief explanation of why these labels were selected (1-2 sentences).")


def get_available_labels(repository: str, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> list:
    """Fetch all available labels for the repository."""
    labels = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/labels?page={page}&per_page=100"
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            print(f"Error fetching repository labels: {response.status_code} - {response.text}", file=sys.stderr)
            break
        data = response.json()
        if not data:
            break
        labels.extend([label["name"] for label in data])
        page += 1
    return labels


def get_issue_comments(
    repository: str,
    issue_number: int,
    headers: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Fetch discussion comments on the issue using pagination."""
    comments: list[dict] = []
    if not repository or not issue_number:
        return comments

    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments?page={page}&per_page=100"
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                print(
                    f"Warning: Failed to fetch issue comments ({response.status_code}): {response.text}",
                    file=sys.stderr,
                )
                break
            data = response.json()
            if not data or not isinstance(data, list):
                break
            comments.extend(data)
            if len(data) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Warning: Exception while fetching issue comments: {e}", file=sys.stderr)
            break
    return comments


def load_triage_prompt(
    issue_title: str,
    issue_body: str,
    available_labels: list,
    issue_comments: list[dict] | None = None,
) -> str:
    """Load prompt guidelines from gemini-triage.toml and substitute template parameters."""
    path = ".github/commands/gemini-triage.toml"
    if not os.path.exists(path):
        action_default_path = os.path.join(os.path.dirname(__file__), "starter-examples", "gemini-triage.toml")
        if os.path.exists(action_default_path):
            path = action_default_path
        else:
            prompt = (
                "You are a helpful issue triage assistant. Categorise the following issue "
                "using only labels from this list: " + ", ".join(available_labels) + "\n\n"
                f"Title: {issue_title}\nBody: {issue_body}"
            )
            if issue_comments:
                comment_lines = [
                    f"- [{((c.get('user') or {}).get('login') or 'user')}]: {c.get('body', '').strip()}"
                    for c in issue_comments
                    if isinstance(c, dict) and str(c.get("body", "")).strip()
                ]
                if comment_lines:
                    prompt += "\n\nDiscussion Comments:\n" + "\n".join(comment_lines)
            return prompt

    with open(path, "rb") as f:
        config = tomllib.load(f)

    prompt = config.get("prompt", "")
    prompt = prompt.replace("!{echo $AVAILABLE_LABELS}", ", ".join(available_labels))
    prompt = prompt.replace("!{echo $ISSUE_TITLE}", issue_title)
    prompt = prompt.replace("!{echo $ISSUE_BODY}", issue_body or "")

    if issue_comments:
        comment_lines = [
            f"- [{((c.get('user') or {}).get('login') or 'user')}]: {c.get('body', '').strip()}"
            for c in issue_comments
            if isinstance(c, dict) and str(c.get("body", "")).strip()
        ]
        if comment_lines:
            prompt += "\n\n**Issue Discussion Comments**:\n```\n" + "\n".join(comment_lines) + "\n```\n"

    return prompt


def apply_labels(
    repository: str, issue_number: int, labels: list, headers: dict, timeout: int = DEFAULT_TIMEOUT
) -> None:
    """Apply the selected labels to the GitHub issue."""
    if not labels:
        print("No labels selected to apply. Skipping API request.", file=sys.stderr)
        return

    url = f"https://api.github.com/repos/{repository}/issues/{issue_number}/labels"
    print(f"Applying labels {labels} to Issue #{issue_number} on {repository}...", file=sys.stderr)
    res = requests.post(url, headers=headers, json={"labels": labels}, timeout=timeout)

    if res.status_code in (200, 201):
        print("Successfully applied labels.", file=sys.stderr)
    else:
        print(f"Error applying labels: {res.status_code} - {res.text}", file=sys.stderr)


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    use_vertexai = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("true", "1")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = get_default_model()

    try:
        timeout = int(os.environ.get("GEMINI_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    headers = {}
    if github_token:
        print("GitHub API Authentication: using GITHUB_TOKEN.", file=sys.stderr)
        headers["Authorization"] = f"token {github_token}"
    else:
        print("GitHub API Authentication: GITHUB_TOKEN not set.", file=sys.stderr)
    headers["Accept"] = "application/vnd.github.v3+json"

    is_dry_run = False
    issue_number = 1
    issue_title = "Example issue title"
    issue_body = "This is a placeholder description of the issue."

    if not event_path or not os.path.exists(event_path):
        print("Warning: GITHUB_EVENT_PATH not set or not found. Running in dry-run/mock mode.", file=sys.stderr)
        is_dry_run = True
    else:
        with open(event_path, "r", encoding="utf-8") as f:
            event_payload = json.load(f)
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")

        if event_name == "issues":
            issue_number = event_payload["issue"]["number"]
            issue_title = event_payload["issue"]["title"]
            issue_body = event_payload["issue"].get("body", "") or ""
        else:
            print(f"Unsupported event type for triage: {event_name}. Running in dry-run mode.", file=sys.stderr)
            is_dry_run = True

    # Gather repository labels and optional discussion comments
    issue_comments: list[dict] = []
    if is_dry_run:
        available_labels = ["bug", "documentation", "enhancement", "duplicate", "help wanted", "good first issue"]
    else:
        available_labels = get_available_labels(repository, headers, timeout=timeout)
        include_comments = os.environ.get("GEMINI_INCLUDE_COMMENT_HISTORY", "true").lower() in ("true", "1", "yes")
        if include_comments:
            issue_comments = get_issue_comments(repository, issue_number, headers, timeout=timeout)
            excluded_authors = parse_excluded_authors(os.environ.get("GEMINI_EXCLUDE_COMMENT_AUTHORS"))
            if excluded_authors:
                issue_comments = filter_comment_authors(issue_comments, excluded_authors)

    if not available_labels:
        print("No repository labels found. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Initialise Gemini client
    if use_vertexai:
        print(f"Initialising GenAI Client (Model: {model_name}) using Vertex AI authentication...", file=sys.stderr)
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        print(
            f"Initialising GenAI Client (Model: {model_name}) using Google AI Studio API Key authentication...",
            file=sys.stderr,
        )
        client = genai.Client(api_key=gemini_api_key)

    triage_prompt = load_triage_prompt(issue_title, issue_body, available_labels, issue_comments=issue_comments)

    # Extract and fetch remote screenshots from issue body and discussion comments
    multimodal_parts = build_triage_multimodal_parts(
        issue_body=issue_body,
        issue_comments=issue_comments,
        github_token=github_token,
        headers=headers,
        timeout=timeout,
    )
    triage_contents: Any = [triage_prompt] + multimodal_parts if multimodal_parts else triage_prompt

    thinking_level = os.environ.get("GEMINI_THINKING_LEVEL")
    thinking_cfg = build_thinking_config(thinking_level)

    triage_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=TriageResult,
        thinking_config=thinking_cfg,
    )

    print("Running issue triage classification...", file=sys.stderr)
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=triage_contents,
            config=triage_config,
        )
    except Exception as gen_err:
        err_msg = str(gen_err).lower()
        if triage_config.thinking_config is not None and (
            "thinking" in err_msg or "400" in err_msg or "invalid_argument" in err_msg
        ):
            print(
                f"Warning: thinking_config not supported by model '{model_name}' ({gen_err}). "
                "Retrying without thinking_config...",
                file=sys.stderr,
            )
            triage_config.thinking_config = None
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=triage_contents,
                    config=triage_config,
                )
            except Exception as retry_err:
                if multimodal_parts:
                    print(
                        f"Warning: Multimodal triage failed ({retry_err}). Falling back to text-only triage...",
                        file=sys.stderr,
                    )
                    response = client.models.generate_content(
                        model=model_name,
                        contents=triage_prompt,
                        config=triage_config,
                    )
                else:
                    raise
        elif multimodal_parts:
            print(
                f"Warning: Multimodal triage failed ({gen_err}). Falling back to text-only triage...",
                file=sys.stderr,
            )
            response = client.models.generate_content(
                model=model_name,
                contents=triage_prompt,
                config=triage_config,
            )
        else:
            raise

    result_text = extract_response_text_or_raise(response)
    result_data = json.loads(result_text)
    triage = TriageResult(**result_data)

    if is_dry_run:
        print("\n=== DRY RUN TRIAGE RESULTS ===", file=sys.stderr)
        print(f"Issue Title: {issue_title}")
        print(f"Chosen Labels: {triage.selected_labels}")
        print(f"Reasoning: {triage.reasoning}\n")
    else:
        apply_labels(repository, issue_number, triage.selected_labels, headers, timeout=timeout)


if __name__ == "__main__":
    main()
