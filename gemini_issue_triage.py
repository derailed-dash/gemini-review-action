# /// script
# dependencies = [
#   "google-genai>=2.10.0",
#   "requests",
#   "pydantic",
# ]
# ///
#!/usr/bin/env python3
"""
Description: Runs an automated GitHub Issue triage using the Google GenAI SDK.
Loads prompt configuration from .github/commands/gemini-triage.toml, fetches
available labels from GitHub API, queries Gemini using Structured Outputs,
and applies the selected labels to the issue.
"""

import json
import os
import sys
import tomllib
from typing import List
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import requests


class TriageResult(BaseModel):
    selected_labels: List[str] = Field(description="List of appropriate labels selected from the available labels list. Must match available labels exactly.")
    reasoning: str = Field(description="A brief explanation of why these labels were selected (1-2 sentences).")


def get_available_labels(repository: str, headers: dict) -> list:
    """Fetch all available labels for the repository."""
    labels = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/labels?page={page}&per_page=100"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Error fetching repository labels: {response.status_code} - {response.text}", file=sys.stderr)
            break
        data = response.json()
        if not data:
            break
        labels.extend([label["name"] for label in data])
        page += 1
    return labels


def load_triage_prompt(issue_title: str, issue_body: str, available_labels: list) -> str:
    """Load prompt guidelines from gemini-triage.toml and substitute template parameters."""
    path = ".github/commands/gemini-triage.toml"
    if not os.path.exists(path):
        return (
            "You are a helpful issue triage assistant. Categorise the following issue "
            "using only labels from this list: " + ", ".join(available_labels) + "\n\n"
            f"Title: {issue_title}\nBody: {issue_body}"
        )

    with open(path, "rb") as f:
        config = tomllib.load(f)

    prompt = config.get("prompt", "")
    prompt = prompt.replace("!{echo $AVAILABLE_LABELS}", ", ".join(available_labels))
    prompt = prompt.replace("!{echo $ISSUE_TITLE}", issue_title)
    prompt = prompt.replace("!{echo $ISSUE_BODY}", issue_body or "")
    return prompt


def apply_labels(repository: str, issue_number: int, labels: list, headers: dict) -> None:
    """Apply the selected labels to the GitHub issue."""
    if not labels:
        print("No labels selected to apply. Skipping API request.", file=sys.stderr)
        return

    url = f"https://api.github.com/repos/{repository}/issues/{issue_number}/labels"
    print(f"Applying labels {labels} to Issue #{issue_number} on {repository}...", file=sys.stderr)
    res = requests.post(url, headers=headers, json={"labels": labels})

    if res.status_code in (200, 201):
        print("Successfully applied labels.", file=sys.stderr)
    else:
        print(f"Error applying labels: {res.status_code} - {res.text}", file=sys.stderr)


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
            issue_body = event_payload["issue"].get("body", "")
        else:
            print(f"Unsupported event type for triage: {event_name}. Running in dry-run mode.", file=sys.stderr)
            is_dry_run = True

    # Gather repository labels
    if is_dry_run:
        available_labels = ["bug", "documentation", "enhancement", "duplicate", "help wanted", "good first issue"]
    else:
        available_labels = get_available_labels(repository, headers)

    if not available_labels:
        print("No repository labels found. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Initialize Gemini client
    print(f"Initializing GenAI Client (Model: {model_name})...", file=sys.stderr)
    if use_vertexai:
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        client = genai.Client(api_key=gemini_api_key)

    triage_prompt = load_triage_prompt(issue_title, issue_body, available_labels)

    print("Running issue triage classification...", file=sys.stderr)
    response = client.models.generate_content(
        model=model_name,
        contents=triage_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TriageResult,
        )
    )

    result_data = json.loads(response.text)
    triage = TriageResult(**result_data)

    if is_dry_run:
        print("\n=== DRY RUN TRIAGE RESULTS ===", file=sys.stderr)
        print(f"Issue Title: {issue_title}")
        print(f"Chosen Labels: {triage.selected_labels}")
        print(f"Reasoning: {triage.reasoning}\n")
    else:
        apply_labels(repository, issue_number, triage.selected_labels, headers)


if __name__ == "__main__":
    main()
