# /// script
# dependencies = [
#   "google-genai>=2.12.1",
#   "requests",
#   "pydantic",
# ]
# ///
#!/usr/bin/env python3
"""
Description: Runs a Pull Request code review using the Google GenAI SDK.
Supports both standard PR events and comment-triggered '/gemini-review' runs.
Includes dry-run mode for local developers to test and run offline.

Outputs and logs (including errors and progress messages) are printed to stderr
and stdout, which are viewable in the GitHub Actions runner execution logs
for the workflow run.
"""

import json
import os
import sys

import requests
from google import genai
from google.genai import types

from gemini_review import (
    DEFAULT_TIMEOUT,
    InlineComment,
    ReviewResult,
    _normalize_model_name,
    build_codebase_context,
    build_pr_diff_prompt,
    build_prompt,
    count_text_tokens,
    filter_review_comments,
    format_pr_comment_history,
    generate_file_tree,
    get_all_repo_files,
    get_file_content,
    get_google_auth_headers,
    get_google_developer_documents,
    get_local_git_files,
    get_pr_comments,
    get_pr_files,
    get_valid_changed_lines,
    is_core_file,
    is_text_file,
    list_available_skills,
    load_config,
    load_skill_instructions,
    load_system_instruction,
    load_workspace_rules,
    parse_skill_metadata,
    post_review,
    search_google_developer_knowledge,
)

# __all__ explicitly marks these imported symbols as public re-exports for backward compatibility.
# This prevents linters (such as Ruff) from pruning unused facade imports needed by tests and external callers.
__all__ = [
    "DEFAULT_TIMEOUT",
    "InlineComment",
    "ReviewResult",
    "_normalize_model_name",
    "build_codebase_context",
    "build_pr_diff_prompt",
    "build_prompt",
    "count_text_tokens",
    "filter_review_comments",
    "format_pr_comment_history",
    "generate_file_tree",
    "get_all_repo_files",
    "get_file_content",
    "get_google_auth_headers",
    "get_google_developer_documents",
    "get_local_git_files",
    "get_pr_comments",
    "get_pr_files",
    "get_valid_changed_lines",
    "is_core_file",
    "is_text_file",
    "list_available_skills",
    "load_config",
    "load_skill_instructions",
    "load_system_instruction",
    "load_workspace_rules",
    "main",
    "parse_skill_metadata",
    "post_review",
    "search_google_developer_knowledge",
]


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    use_vertexai = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("true", "1")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = os.environ.get("GEMINI_MODEL", os.environ.get("MODEL", "gemini-3.6-flash"))

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
                print(
                    f"User association '{author_association}' not authorized to trigger code review. Exiting.",
                    file=sys.stderr,
                )
                sys.exit(0)

            if "pull_request" not in event_payload["issue"]:
                print("Comment is not on a pull request. Exiting.", file=sys.stderr)
                sys.exit(0)

            pr_number = event_payload["issue"]["number"]
            url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"
            res = requests.get(url, headers=headers, timeout=timeout)
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
        files = get_pr_files(repository, pr_number, headers, timeout=timeout)

    if not files:
        print("No files modified in this PR. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Filter out excluded file types
    text_files = [f for f in files if is_text_file(f["filename"])]
    if not text_files:
        print("No text-based files to review. Exiting.", file=sys.stderr)
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

    config = load_config()
    system_instruction = load_system_instruction(repository, pr_number, config)

    # Load workspace rules (AGENTS.md, etc.)
    workspace_rules = load_workspace_rules()
    if workspace_rules:
        system_instruction += f"\n\n## Project Rules & Best Practices:\n{workspace_rules}"

    # Assemble tools list
    tools = [list_available_skills, load_skill_instructions]
    auth_headers = get_google_auth_headers()
    disable_dev_k = os.environ.get("DISABLE_DEVELOPER_KNOWLEDGE", "false").lower() == "true"
    has_dev_knowledge = bool(
        not disable_dev_k and auth_headers and ("X-Goog-Api-Key" in auth_headers or "Authorization" in auth_headers)
    )
    if has_dev_knowledge:
        print("Registering Google Developer Knowledge MCP tools...", file=sys.stderr)
        tools.extend([search_google_developer_knowledge, get_google_developer_documents])

    # Add tools info to system instruction
    system_instruction += "\n\n## Tools Availability:"
    system_instruction += (
        "\n- You have workspace skill tools: `list_available_skills` and `load_skill_instructions` to find and load"
        " local project guidelines."
    )
    if has_dev_knowledge:
        system_instruction += (
            "\n- You have Google Developer Knowledge search tools: `search_google_developer_knowledge` and"
            " `get_google_developer_documents` to query official Google APIs, Google Cloud, Firebase, and other"
            " developer docs."
        )

    include_comments_env = os.environ.get("GEMINI_INCLUDE_COMMENT_HISTORY", "true").lower() in ("true", "1")
    include_comments_config = config.get("include_comment_history", True)
    should_include_comments = include_comments_env and include_comments_config

    comment_history_str = ""
    comment_history_tokens = 0
    if should_include_comments and not is_dry_run and repository and pr_number:
        print(f"Fetching prior PR comments for PR #{pr_number}...", file=sys.stderr)
        review_comments, issue_comments = get_pr_comments(repository, pr_number, headers, timeout=timeout)
        comment_history_str = format_pr_comment_history(review_comments, issue_comments)
        if comment_history_str:
            comment_history_tokens = count_text_tokens(client, model_name, comment_history_str)
            print(
                f"PR comment history included ({comment_history_tokens:,} tokens).",
                file=sys.stderr,
            )

    pr_diff_prompt = build_pr_diff_prompt(text_files)
    dynamic_pr_prompt = f"{pr_diff_prompt}\n\n{comment_history_str}" if comment_history_str else pr_diff_prompt
    codebase_context = build_codebase_context(text_files, config)
    full_prompt = f"{dynamic_pr_prompt}\n\n{codebase_context}" if codebase_context else dynamic_pr_prompt

    enable_caching = config.get("enable_context_caching", True)
    cache_ttl_seconds = config.get("cache_ttl_seconds", 3600)
    cache_ttl = f"{cache_ttl_seconds}s"

    cached_content_name = None
    contents_to_send = full_prompt

    if enable_caching and hasattr(client, "caches") and codebase_context:
        try:
            # Gemini Context Caching requires minimum 32,768 tokens (approx 100,000+ characters)
            if len(codebase_context) > 100000:
                clean_repo = repository.replace("/", "-").replace("\\", "-") if repository else "repo"
                clean_model = _normalize_model_name(model_name).replace("/", "-").replace("\\", "-")
                display_name = f"repo-cache-{clean_repo}-{clean_model}"
                legacy_display_name = f"repo-cache-{clean_repo}"

                # Check if an active cache already exists matching display_name and model_name
                existing_cache = None
                try:
                    active_caches = client.caches.list()
                    for cache_item in active_caches:
                        item_display_name = getattr(cache_item, "display_name", None)
                        if item_display_name in (display_name, legacy_display_name):
                            item_model = getattr(cache_item, "model", None)
                            if isinstance(item_model, str) and _normalize_model_name(
                                item_model
                            ) != _normalize_model_name(model_name):
                                print(
                                    f"Notice: Found cache ({item_display_name}: {cache_item.name}) "
                                    f"for a different model ('{item_model}', expected '{model_name}'). "
                                    "Skipping cache reuse.",
                                    file=sys.stderr,
                                )
                                continue
                            existing_cache = cache_item
                            break
                except Exception as list_err:
                    print(f"Notice: Cache listing failed ({list_err}), creating fresh cache.", file=sys.stderr)

                if existing_cache:
                    cached_content_name = existing_cache.name
                    contents_to_send = dynamic_pr_prompt
                    active_display_name = getattr(existing_cache, "display_name", display_name)
                    print(
                        f"Reusing active Gemini context cache ({active_display_name}: {cached_content_name})...",
                        file=sys.stderr,
                    )
                else:
                    print(f"Creating Gemini context cache ({display_name})...", file=sys.stderr)
                    parsed_tools = None
                    if tools:
                        try:
                            parsed_cfg = client.models._parse_config(types.GenerateContentConfig(tools=tools))
                            parsed_tools = parsed_cfg.tools
                        except Exception:
                            parsed_tools = None

                    cache_obj = client.caches.create(
                        model=model_name,
                        config=types.CreateCachedContentConfig(
                            contents=[codebase_context],
                            display_name=display_name,
                            system_instruction=system_instruction,
                            tools=parsed_tools,
                            ttl=cache_ttl,
                        ),
                    )
                    cached_content_name = cache_obj.name
                    contents_to_send = dynamic_pr_prompt
                    print(f"Context cache active: {cached_content_name}", file=sys.stderr)
        except Exception as e:
            print(
                f"Warning: Context caching unavailable or skipped ({e}). Proceeding with direct context.",
                file=sys.stderr,
            )
            cached_content_name = None
            contents_to_send = full_prompt

    if cached_content_name:
        gen_config = types.GenerateContentConfig(
            cached_content=cached_content_name,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        )
    else:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        )

    print("Generating code review...", file=sys.stderr)

    try:
        response = client.models.generate_content(
            model=model_name,
            contents=contents_to_send,
            config=gen_config,
        )
    except Exception as gen_err:
        if cached_content_name:
            print(
                f"Warning: generate_content with cached content failed ({gen_err}). "
                "Falling back to direct context generation...",
                file=sys.stderr,
            )
            cached_content_name = None
            contents_to_send = full_prompt
            gen_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                response_mime_type="application/json",
                response_schema=ReviewResult,
            )
            response = client.models.generate_content(
                model=model_name,
                contents=contents_to_send,
                config=gen_config,
            )
        else:
            raise

    usage_dict = None
    if response.usage_metadata:
        usage = response.usage_metadata
        prompt_tokens = usage.prompt_token_count or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
        candidates_tokens = usage.candidates_token_count or 0
        total_tokens = usage.total_token_count or 0

        fresh_tokens = max(0, prompt_tokens - cached_tokens - comment_history_tokens)
        cache_percentage = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0.0

        usage_dict = {
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "candidates_tokens": candidates_tokens,
            "comment_history_tokens": comment_history_tokens,
            "fresh_tokens": fresh_tokens,
            "total_tokens": total_tokens,
            "cache_percentage": cache_percentage,
        }

        cache_str = f" ({cache_percentage:.1f}% cached)" if cached_tokens > 0 else ""
        print(
            f"Token Usage: {prompt_tokens:,d} input tokens{cache_str}, {candidates_tokens:,d} output tokens."
            f" Total: {total_tokens:,d} tokens.",
            file=sys.stderr,
        )

    review_data = json.loads(response.text)

    review = ReviewResult(**review_data)
    review = filter_review_comments(review, text_files)

    if is_dry_run:
        print("\n=== DRY RUN REVIEW SUMMARY ===", file=sys.stderr)
        print(f"Summary: {review.summary}")
        if review.resolved_items:
            print("\n=== RESOLVED ITEMS ===", file=sys.stderr)
            for r in review.resolved_items:
                print(f"✅ {r}")
        print("\n=== GENERAL FEEDBACK ===", file=sys.stderr)
        for gf in review.general_feedback:
            print(f"- {gf}")
        print("\n=== INLINE COMMENTS ===", file=sys.stderr)
        for c in review.comments:
            suggestion_str = f"\nSuggestion:\n{c.code_suggestion}" if c.code_suggestion else ""
            print(f"File: {c.path}:{c.line} ({c.side}) - Severity: {c.severity}\n{c.comment_text}{suggestion_str}\n")
    else:
        post_review(
            repository,
            pr_number,
            head_sha,
            review,
            headers,
            timeout=timeout,
            usage_metadata=usage_dict,
        )


if __name__ == "__main__":
    main()
