#!/usr/bin/env python3
"""
Description: Runs an automated GitHub Issue triage using the Google GenAI SDK.
By default on the Gemini Developer API, uses the Gemini Decisions API
(``gemini-decision-1`` on ``v1beta``) to score every repository label (with
descriptions) and an actionability guardrail in a single zero-output-token
prefill pass, falling back automatically to ``generate_content`` Structured Outputs
on Vertex AI, custom ``.github/commands/gemini-triage.toml`` overrides, >990 labels,
>262,144 estimated tokens, or API/safety errors.

Outputs and logs (including errors, per-label probabilities, and cost/latency
telemetry) are printed to stderr and stdout, which are viewable in the GitHub
Actions runner execution logs for the workflow run.
"""

import json
import os
import sys
import time
import tomllib
from collections.abc import Mapping
from typing import Any

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from gemini_review import (
    DECISIONS_INPUT_TOKEN_LIMIT,
    MAX_TRIAGE_DECISIONS_LABELS,
    build_decision_input_blocks,
    build_thinking_config,
    build_triage_multimodal_parts,
    call_decisions,
    create_genai_client,
    estimate_cost,
    estimate_decision_tokens,
    extract_remote_image_urls,
    extract_response_text_or_raise,
    fetch_remote_image,
    filter_comment_authors,
    generate_content_with_stream_fallback,
    get_default_model,
    get_service_tier,
    get_service_tier_fallback,
    get_triage_engine,
    get_triage_media_resolution,
    get_triage_threshold,
    is_media_resolution_error,
    is_thinking_config_error,
    parse_excluded_authors,
    strip_media_resolution,
    usd,
)

DEFAULT_TIMEOUT = 60
ACTIONABLE_THRESHOLD = 0.50

DEFAULT_DRY_RUN_LABELS: list[dict[str, str]] = [
    {"name": "bug", "description": "Something isn't working"},
    {"name": "documentation", "description": "Improvements or additions to documentation"},
    {"name": "enhancement", "description": "New feature or request"},
    {"name": "duplicate", "description": "This issue or pull request already exists"},
    {"name": "help wanted", "description": "Extra attention is needed"},
    {"name": "good first issue", "description": "Good for newcomers"},
]

__all__ = [
    "ACTIONABLE_THRESHOLD",
    "DEFAULT_DRY_RUN_LABELS",
    "TriageResult",
    "get_available_labels",
    "get_available_label_metadata",
    "get_issue_comments",
    "has_custom_triage_toml",
    "load_triage_prompt",
    "build_triage_decision_input_text",
    "build_triage_decision_questions",
    "evaluate_triage_decisions",
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


def get_available_label_metadata(
    repository: str,
    headers: dict,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, str]]:
    """Fetch all available labels and descriptions for the repository."""
    labels: list[dict[str, str]] = []
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
        for label in data:
            if isinstance(label, dict) and label.get("name"):
                labels.append(
                    {
                        "name": str(label["name"]),
                        "description": str(label.get("description") or "").strip(),
                    }
                )
        page += 1
    return labels


def get_available_labels(repository: str, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> list:
    """Fetch all available label names for the repository."""
    return [item["name"] for item in get_available_label_metadata(repository, headers, timeout=timeout)]


_DEFAULT_GET_AVAILABLE_LABELS = get_available_labels


def has_custom_triage_toml(path: str = ".github/commands/gemini-triage.toml") -> bool:
    """Return True when the repository defines a custom triage TOML override."""
    return os.path.isfile(path)


def _format_issue_comments_text(issue_comments: list[dict] | None) -> str:
    """Format non-empty issue discussion comments into plain text lines."""
    if not issue_comments:
        return ""
    comment_lines = [
        f"- [{((c.get('user') or {}).get('login') or 'user')}]: {str(c.get('body', '')).strip()}"
        for c in issue_comments
        if isinstance(c, dict) and str(c.get("body", "")).strip()
    ]
    return "\n".join(comment_lines)


def build_triage_decision_input_text(
    issue_title: str,
    issue_body: str,
    issue_comments: list[dict] | None = None,
) -> str:
    """Assemble compact issue text input for the Gemini Decisions API."""
    parts = [
        f"Issue Title: {issue_title}",
        f"Issue Body:\n{issue_body or ''}",
    ]
    comments_text = _format_issue_comments_text(issue_comments)
    if comments_text:
        parts.append(f"Issue Discussion Comments:\n{comments_text}")
    return "\n\n".join(parts)


def build_triage_decision_questions(
    label_metadata: list[dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Build collision-safe ``noul`` questions for actionability and each repository label."""
    questions: dict[str, dict[str, Any]] = {
        "is_actionable_issue": {
            "type": "noul",
            "instructions": (
                "Determine whether this GitHub issue contains enough genuine signal (in its title, body, "
                "discussion comments, or attached screenshots) to be triaged accurately, rather than being "
                "extremely sparse, unclear, or a placeholder/test issue."
            ),
            "criteria": {
                "true_description": (
                    "The issue describes an understandable bug, question, request, or diagnostic context "
                    "(via text or attached screenshots)."
                ),
                "false_description": (
                    "The issue is extremely sparse, unclear, blank, or a simple placeholder/test issue "
                    "with no informative screenshots."
                ),
            },
        }
    }
    key_to_label: dict[str, str] = {}
    for idx, entry in enumerate(label_metadata):
        key = f"label_{idx}"
        name = entry["name"]
        desc = entry.get("description", "").strip()
        desc_clause = f" Label description: {desc}." if desc else ""
        questions[key] = {
            "type": "noul",
            "instructions": (
                f"Should the repository label '{name}' be applied to this GitHub issue?{desc_clause} "
                "Evaluate the issue title, body, discussion comments, and any attached screenshots."
            ),
        }
        key_to_label[key] = name
    return questions, key_to_label


def _extract_noul_scores(answer: Any) -> tuple[float, float]:
    """Extract ``(p_yes, confidence)`` from a Decisions ``NoulAnswer`` or dict."""
    if isinstance(answer, Mapping):
        raw_p = answer.get("noul", 0.0)
        raw_conf = answer.get("confidence", 0.0)
    else:
        raw_p = getattr(answer, "noul", 0.0)
        raw_conf = getattr(answer, "confidence", 0.0)
    return float(raw_p), float(raw_conf)


def evaluate_triage_decisions(
    client: Any,
    *,
    issue_title: str,
    issue_body: str,
    label_metadata: list[dict[str, str]],
    issue_comments: list[dict] | None = None,
    multimodal_parts: list[Any] | None = None,
    threshold: float = 0.70,
    image_resolution: str = "medium",
) -> tuple[list[str], dict[str, Any]] | None:
    """Classify an issue via ``gemini-decision-1``, returning ``None`` to trigger fallback."""
    if len(label_metadata) > MAX_TRIAGE_DECISIONS_LABELS:
        print(
            f"Warning: Repository has {len(label_metadata)} labels, exceeding Decisions API triage cap "
            f"({MAX_TRIAGE_DECISIONS_LABELS}). Falling back to generate_content...",
            file=sys.stderr,
        )
        return None

    input_text = build_triage_decision_input_text(issue_title, issue_body, issue_comments)
    input_blocks = build_decision_input_blocks(
        input_text,
        multimodal_parts,
        image_resolution=image_resolution,
    )
    questions, key_to_label = build_triage_decision_questions(label_metadata)

    est_tokens = estimate_decision_tokens(input_blocks, questions)
    if est_tokens > DECISIONS_INPUT_TOKEN_LIMIT:
        print(
            f"Warning: Estimated Decisions input ({est_tokens:,} tokens) exceeds {DECISIONS_INPUT_TOKEN_LIMIT:,} "
            "token ceiling. Falling back to generate_content...",
            file=sys.stderr,
        )
        return None

    try:
        result = call_decisions(client, input_blocks=input_blocks, questions=questions)
    except Exception as err:
        print(
            f"Warning: Decisions API issue triage failed ({err}). Falling back to generate_content...",
            file=sys.stderr,
        )
        return None

    answers = result.answers
    actionable_answer = answers.get("is_actionable_issue")
    if actionable_answer is not None:
        actionable_p, actionable_conf = _extract_noul_scores(actionable_answer)
        actionable = actionable_p >= ACTIONABLE_THRESHOLD
    else:
        actionable_p, actionable_conf = 1.0, 1.0
        actionable = True

    scored: list[tuple[int, str, float, float]] = []
    for idx, (key, name) in enumerate(key_to_label.items()):
        label_answer = answers.get(key)
        if label_answer is None:
            continue
        p_yes, conf = _extract_noul_scores(label_answer)
        scored.append((idx, name, p_yes, conf))

    scored.sort(key=lambda item: (-item[2], item[0]))
    selected = [name for _, name, p_yes, _ in scored if actionable and p_yes >= threshold]
    selected_set = set(selected)

    print(
        f"Triage telemetry: Engine=decisions ({result.model}) | latency={result.elapsed_ms:.1f}ms | "
        f"input_tokens={result.input_tokens:,} | cost={usd(result.cost_usd)} | "
        f"actionable={actionable} (P(yes)={actionable_p:.2f}, conf={actionable_conf:.2f}) | "
        f"threshold={threshold:.2f}",
        file=sys.stderr,
    )
    for _, name, p_yes, conf in scored:
        status = "SELECTED" if name in selected_set else "skipped"
        print(f"  - {name}: P(yes)={p_yes:.2f}, conf={conf:.2f} [{status}]", file=sys.stderr)

    summary = {
        "engine": "decisions",
        "model": result.model,
        "elapsed_ms": result.elapsed_ms,
        "input_tokens": result.input_tokens,
        "total_tokens": result.total_tokens,
        "cost_usd": result.cost_usd,
        "actionable": actionable,
        "actionable_p": actionable_p,
        "actionable_conf": actionable_conf,
        "threshold": threshold,
        "scores": [
            {
                "name": name,
                "p_yes": p_yes,
                "confidence": conf,
                "selected": name in selected_set,
            }
            for _, name, p_yes, conf in scored
        ],
    }
    return selected, summary


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


def _safe_usage_int(usage: Any, attr: str) -> int:
    """Return non-negative integer usage attribute or 0, rejecting bools and mocks."""
    val = getattr(usage, attr, None) if usage is not None else None
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return 0


def _filter_allowlisted_labels(selected_labels: list[str], available_labels: list[str]) -> list[str]:
    """Deduplicate selected labels in order and drop any not present in the repository allowlist."""
    valid_label_set = set(available_labels)
    unique_selected = list(dict.fromkeys(selected_labels))
    dropped_labels = [lbl for lbl in unique_selected if lbl not in valid_label_set]
    if dropped_labels:
        print(
            f"Warning: Dropping unrecognised labels not in repository allowlist: {dropped_labels}",
            file=sys.stderr,
        )
    return [lbl for lbl in unique_selected if lbl in valid_label_set]


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = get_default_model()
    triage_engine = get_triage_engine()
    triage_threshold = get_triage_threshold()
    triage_resolution = get_triage_media_resolution()

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

    # Gather repository labels (with descriptions) and optional discussion comments
    issue_comments: list[dict] = []
    if is_dry_run:
        label_metadata = list(DEFAULT_DRY_RUN_LABELS)
    else:
        if get_available_labels is not _DEFAULT_GET_AVAILABLE_LABELS:
            label_metadata = [
                {"name": name, "description": ""} for name in get_available_labels(repository, headers, timeout=timeout)
            ]
        else:
            label_metadata = get_available_label_metadata(repository, headers, timeout=timeout)
        include_comments = os.environ.get("GEMINI_INCLUDE_COMMENT_HISTORY", "true").lower() in ("true", "1", "yes")
        if include_comments:
            issue_comments = get_issue_comments(repository, issue_number, headers, timeout=timeout)
            excluded_authors = parse_excluded_authors(os.environ.get("GEMINI_EXCLUDE_COMMENT_AUTHORS"))
            if excluded_authors:
                issue_comments = filter_comment_authors(issue_comments, excluded_authors)

    available_labels = [item["name"] for item in label_metadata]
    if not available_labels:
        print("No repository labels found. Exiting.", file=sys.stderr)
        sys.exit(0)

    requested_service_tier = get_service_tier()
    service_tier_fallback = get_service_tier_fallback()

    # Initialise Gemini client (enforce >=300s / 600s Flex timeout floor and Developer API v1beta).
    client, use_vertexai = create_genai_client(
        model_name=model_name,
        timeout=timeout,
        api_key=gemini_api_key,
        project=project,
        location=location,
        service_tier=requested_service_tier,
        client_cls=genai.Client,
    )
    sdk_service_tier = types.ServiceTier.FLEX if (not use_vertexai and requested_service_tier == "flex") else None

    # Extract and fetch remote screenshots from issue body and discussion comments once for either engine
    multimodal_parts = build_triage_multimodal_parts(
        issue_body=issue_body,
        issue_comments=issue_comments,
        github_token=github_token,
        headers=headers,
        timeout=timeout,
    )

    should_try_decisions = False
    if triage_engine == "generate_content":
        print("Issue triage engine set to 'generate_content'. Skipping Decisions API.", file=sys.stderr)
    elif use_vertexai:
        print(
            "Warning: Decisions API is currently Gemini Developer API (v1beta) only; "
            "falling back to generate_content on Vertex AI.",
            file=sys.stderr,
        )
    elif triage_engine == "auto" and has_custom_triage_toml():
        print(
            "Custom .github/commands/gemini-triage.toml detected with triage_engine='auto'; "
            "falling back to generate_content to honour custom prompt.",
            file=sys.stderr,
        )
    else:
        should_try_decisions = True

    if should_try_decisions:
        print("Running issue triage classification via Decisions API (gemini-decision-1)...", file=sys.stderr)
        decisions_outcome = evaluate_triage_decisions(
            client,
            issue_title=issue_title,
            issue_body=issue_body,
            label_metadata=label_metadata,
            issue_comments=issue_comments,
            multimodal_parts=multimodal_parts,
            threshold=triage_threshold,
            image_resolution=triage_resolution,
        )
        if decisions_outcome is not None:
            selected_labels, summary = decisions_outcome
            filtered_labels = _filter_allowlisted_labels(selected_labels, available_labels)
            if is_dry_run:
                print("\n=== DRY RUN TRIAGE RESULTS ===", file=sys.stderr)
                print(f"Issue Title: {issue_title}")
                print(f"Engine: decisions ({summary['model']})")
                print(f"Chosen Labels: {filtered_labels}")
                print(
                    f"Telemetry: {summary['elapsed_ms']:.1f}ms, "
                    f"{summary['input_tokens']:,} input tokens, {usd(summary['cost_usd'])}"
                )
                print(
                    f"Actionable: {summary['actionable']} "
                    f"(P(yes)={summary['actionable_p']:.2f}, conf={summary['actionable_conf']:.2f})"
                )
                for item in summary["scores"]:
                    marker = "*" if item["selected"] else " "
                    print(f"  [{marker}] {item['name']}: P(yes)={item['p_yes']:.2f}, conf={item['confidence']:.2f}")
            else:
                apply_labels(repository, issue_number, filtered_labels, headers, timeout=timeout)
            return

    triage_prompt = load_triage_prompt(issue_title, issue_body, available_labels, issue_comments=issue_comments)
    triage_contents: Any = [triage_prompt] + multimodal_parts if multimodal_parts else triage_prompt

    thinking_level = os.environ.get("GEMINI_THINKING_LEVEL")
    thinking_cfg = build_thinking_config(thinking_level, model=model_name)

    triage_config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=TriageResult,
        thinking_config=thinking_cfg,
        service_tier=sdk_service_tier,
    )

    print("Running issue triage classification...", file=sys.stderr)
    started = time.perf_counter()
    response = None
    try:
        response = generate_content_with_stream_fallback(
            client,
            model=model_name,
            contents=triage_contents,
            config=triage_config,
            service_tier_fallback=service_tier_fallback,
        )
    except Exception as gen_err:
        last_err: Exception = gen_err

        def _retry_without_thinking_config() -> bool:
            nonlocal response, last_err
            if response is not None or triage_config.thinking_config is None or not is_thinking_config_error(last_err):
                return False
            print(
                f"Warning: thinking_config not supported by model '{model_name}' ({last_err}). "
                "Retrying without thinking_config...",
                file=sys.stderr,
            )
            triage_config.thinking_config = None
            try:
                response = generate_content_with_stream_fallback(
                    client,
                    model=model_name,
                    contents=triage_contents,
                    config=triage_config,
                    service_tier_fallback=service_tier_fallback,
                )
            except Exception as err_after_thinking:
                last_err = err_after_thinking
            return True

        # Stage 1: Retry without thinking_config only when error indicates unsupported thinking config
        _retry_without_thinking_config()

        # Stage 2: Strip per-part media_resolution and retry with screenshots on 400 / INVALID_ARGUMENT
        if response is None and multimodal_parts and is_media_resolution_error(last_err):
            stripped_parts, had_res = strip_media_resolution(multimodal_parts)
            if had_res:
                print(
                    f"Warning: Model rejected per-part media_resolution ({last_err}). "
                    "Retrying multimodal triage without per-part media_resolution...",
                    file=sys.stderr,
                )
                multimodal_parts = stripped_parts
                triage_contents = [triage_prompt] + multimodal_parts
                try:
                    response = generate_content_with_stream_fallback(
                        client,
                        model=model_name,
                        contents=triage_contents,
                        config=triage_config,
                        service_tier_fallback=service_tier_fallback,
                    )
                except Exception as err_after_strip:
                    last_err = err_after_strip
                    _retry_without_thinking_config()

        # Stage 3: Fall back to text-only triage when multimodal parts fail (skip on auth/permission/quota errors)
        err_msg = str(last_err).lower()
        auth_or_quota_markers = (
            "api key",
            "api_key",
            "permission denied",
            "401",
            "403",
            "429",
            "resource_exhausted",
            "quota",
        )
        if response is None and multimodal_parts and not any(m in err_msg for m in auth_or_quota_markers):
            print(
                f"Warning: Multimodal triage failed ({last_err}). Falling back to text-only triage...",
                file=sys.stderr,
            )
            response = generate_content_with_stream_fallback(
                client,
                model=model_name,
                contents=triage_prompt,
                config=triage_config,
                service_tier_fallback=service_tier_fallback,
            )

        if response is None:
            raise last_err

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    usage_meta = getattr(response, "usage_metadata", None)
    prompt_tokens = _safe_usage_int(usage_meta, "prompt_token_count")
    cached_tokens = _safe_usage_int(usage_meta, "cached_content_token_count")
    candidates_tokens = _safe_usage_int(usage_meta, "candidates_token_count")
    thoughts_tokens = _safe_usage_int(usage_meta, "thoughts_token_count")
    if prompt_tokens or candidates_tokens or thoughts_tokens:
        fresh_tokens = max(0, prompt_tokens - cached_tokens)
        cost = estimate_cost(
            {
                "fresh_tokens": fresh_tokens,
                "cached_tokens": cached_tokens,
                "candidates_tokens": candidates_tokens,
                "thoughts_tokens": thoughts_tokens,
                "service_tier": "flex" if sdk_service_tier == types.ServiceTier.FLEX else "standard",
                "vertex_location": location if use_vertexai else "",
            },
            model=model_name,
        )
        print(
            f"Triage telemetry: Engine=generate_content ({model_name}) | latency={elapsed_ms:.1f}ms | "
            f"input_tokens={prompt_tokens:,} | output_tokens={candidates_tokens:,} | "
            f"thinking_tokens={thoughts_tokens:,} | cost={usd(cost.total)}",
            file=sys.stderr,
        )

    result_text = extract_response_text_or_raise(response)
    result_data = json.loads(result_text)
    triage = TriageResult(**result_data)

    filtered_labels = _filter_allowlisted_labels(triage.selected_labels, available_labels)

    if is_dry_run:
        print("\n=== DRY RUN TRIAGE RESULTS ===", file=sys.stderr)
        print(f"Issue Title: {issue_title}")
        print(f"Engine: generate_content ({model_name})")
        print(f"Chosen Labels: {filtered_labels}")
        print(f"Reasoning: {triage.reasoning}\n")
    else:
        apply_labels(repository, issue_number, filtered_labels, headers, timeout=timeout)


if __name__ == "__main__":
    main()
