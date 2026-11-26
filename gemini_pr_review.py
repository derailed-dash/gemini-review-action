#!/usr/bin/env python3
"""
Description: Runs a Pull Request code review using the Google GenAI SDK.
Supports both standard PR events and comment-triggered '/gemini-review' runs.
Includes dry-run mode for local developers to test and run offline.

Outputs and logs (including errors and progress messages) are printed to stderr
and stdout, which are viewable in the GitHub Actions runner execution logs
for the workflow run.
"""

import inspect
import json
import os
import sys
from typing import Any

import requests
from google import genai
from google.genai import types

import gemini_review as gr
from gemini_review.repo import _normalise_relative_posix


def __getattr__(name: str):
    """Fallback to gemini_review for backward compatibility and test mock resolution."""
    return getattr(gr, name)


BASELINE_CONTEXT_ACK = (
    "Baseline repository context received. Please provide the Pull Request diff and discussion history to review."
)


def _to_part(item: Any) -> types.Part:
    """Coerce a raw string or types.Part into a types.Part instance for explicit Content turns."""
    if isinstance(item, types.Part):
        return item
    return types.Part.from_text(text=item if isinstance(item, str) else str(item))


def _assemble_direct_contents(
    codebase_context: str,
    codebase_multimodal_parts: list[Any],
    dynamic_pr_prompt: str,
    visual_diff_parts: list[Any],
) -> str | list[Any]:
    """Assemble uncached request contents with a strict multi-turn prefix cache boundary.

    ARCHITECTURAL INVARIANT — DO NOT FLATTEN BACK INTO A SINGLE `UserContent`:
    Empirical Vertex AI A/B benchmarks on `gemini-4-hydrogen` (`eu` and `global`) proved that
    collapsing `[codebase_context, *codebase_multimodal_parts, dynamic_pr_prompt]` into one
    single `Content(role="user")` turn drops implicit cache hit rates to ~2.3%–14.7% (caching only
    `system_instruction + tools` and re-billing the entire ~250k+ token codebase baseline whenever
    a new commit or comment changes the tail — even text-only single-turn prompts cap around 78.3%).

    Isolating the invariant baseline (`codebase_context` + `codebase_multimodal_parts`) in Turn 0
    (`role="user"`), followed by a fixed Turn 1 (`role="model"`, `BASELINE_CONTEXT_ACK`), followed by
    Turn 2 (`role="user"`, `dynamic_pr_prompt` + `visual_diff_parts`) creates a clean turn boundary
    that achieves ~95%+ implicit prefix cache hits across commits, `/gemini-review` reruns, and
    concurrent PRs on both `global` and `eu`/`us` endpoints.
    """
    if codebase_context or codebase_multimodal_parts:
        baseline_parts: list[types.Part] = []
        if codebase_context:
            baseline_parts.append(types.Part.from_text(text=codebase_context))
        for item in codebase_multimodal_parts:
            baseline_parts.append(_to_part(item))

        dynamic_parts: list[types.Part] = []
        if dynamic_pr_prompt:
            dynamic_parts.append(types.Part.from_text(text=dynamic_pr_prompt))
        for item in visual_diff_parts:
            dynamic_parts.append(_to_part(item))
        if not dynamic_parts:
            dynamic_parts.append(types.Part.from_text(text="Please review the attached repository changes."))

        return [
            types.Content(role="user", parts=baseline_parts),
            types.Content(role="model", parts=[types.Part.from_text(text=BASELINE_CONTEXT_ACK)]),
            types.Content(role="user", parts=dynamic_parts),
        ]

    # When there is no static baseline context (e.g. diff_only mode or Stage 1 budget fallback),
    # keep the prompt as a single flat string or multimodal list to minimise token overhead.
    if not visual_diff_parts:
        return dynamic_pr_prompt
    if not dynamic_pr_prompt:
        return list(visual_diff_parts)
    return [dynamic_pr_prompt, *visual_diff_parts]


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = gr.get_default_model()

    try:
        timeout = int(os.environ.get("GEMINI_TIMEOUT", str(gr.DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = gr.DEFAULT_TIMEOUT

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
    base_sha = "main"
    event_name = os.environ.get("GITHUB_EVENT_NAME", "pull_request")

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
            base_sha = event_payload["pull_request"].get("base", {}).get("sha", "main")

            skip_suggestions = os.environ.get("GEMINI_SKIP_INLINE_SUGGESTIONS", "true").lower() in ("true", "1")

            if skip_suggestions and gr.is_inline_suggestion_commit(repository, head_sha, headers, timeout=timeout):
                print(
                    f"Head commit {head_sha[:7]} was created by accepting an inline suggestion via GitHub UI. "
                    "Skipping automated re-review to avoid unnecessary review noise.",
                    file=sys.stderr,
                )
                sys.exit(0)
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
            base_sha = pr_data.get("base", {}).get("sha", "main")
        else:
            print(f"Unsupported event type: {event_name}. Running in dry-run mode.", file=sys.stderr)
            is_dry_run = True

    # Gather file patches and full contents
    if is_dry_run:
        print("Gathering files from local git tree...", file=sys.stderr)
        files = gr.get_local_git_files()
    else:
        print(f"Fetching files for PR #{pr_number} from GitHub API...", file=sys.stderr)
        files = gr.get_pr_files(repository, pr_number, headers, timeout=timeout)

    if not files:
        print("No files modified in this PR. Exiting.", file=sys.stderr)
        sys.exit(0)

    diff_exclude_patterns = gr.get_diff_exclude_patterns()
    excluded_diff_files: list[str] = []
    if diff_exclude_patterns:
        files, excluded_diff_files = gr.filter_excluded_diff_files(files, diff_exclude_patterns)
        if excluded_diff_files:
            sample = ", ".join(excluded_diff_files[:5])
            suffix = f" (+{len(excluded_diff_files) - 5} more)" if len(excluded_diff_files) > 5 else ""
            print(
                f"Diff exclusion: excluded {len(excluded_diff_files)} PR file(s) matching "
                f"diff_exclude_patterns: {sample}{suffix}",
                file=sys.stderr,
            )
        if not files:
            print("All modified files in this PR were excluded by diff_exclude_patterns. Exiting.", file=sys.stderr)
            sys.exit(0)

    # Filter text and image files
    text_files = [f for f in files if gr.is_text_file(f["filename"])]
    image_files = [f for f in files if gr.is_supported_image(f["filename"])]
    omitted_non_text_files = [
        f"{f['filename']} ({f.get('status', 'modified')})"
        for f in files
        if not gr.is_text_file(f["filename"]) and not gr.is_supported_image(f["filename"])
    ]
    if not text_files and not image_files:
        print("No text-based or visual image files to review. Exiting.", file=sys.stderr)
        sys.exit(0)

    config = gr.load_config()
    requested_service_tier = gr.get_service_tier(config)
    service_tier_fallback = gr.get_service_tier_fallback(config)

    # Initialise Gemini client (enforce >=300s / 600s Flex timeout floor and Developer API v1beta).
    client, use_vertexai = gr.create_genai_client(
        model_name=model_name,
        timeout=timeout,
        api_key=gemini_api_key,
        project=project,
        location=location,
        service_tier=requested_service_tier,
        client_cls=genai.Client,
    )
    effective_service_tier = "standard" if use_vertexai else requested_service_tier
    sdk_service_tier = types.ServiceTier.FLEX if effective_service_tier == "flex" else None
    tier_state: dict[str, Any] = {
        "effective_service_tier": effective_service_tier,
        "flex_fallback": False,
    }

    # Cost attribution. Vertex attaches these to the billed charge so spend can be grouped
    # by repository in the Cloud Billing export; returns None on the API-key path, where the
    # API has no labels field at all.
    billing_labels = gr.build_labels(client, config, repository)
    if billing_labels:
        print(f"Billing labels: {billing_labels}", file=sys.stderr)
    custom_instructions = gr.load_custom_instructions()
    system_instruction = gr.load_system_instruction(
        repository, pr_number, config, custom_instructions=custom_instructions
    )

    # Assemble tools list
    tools = [gr.list_available_skills, gr.load_skill_instructions]
    auth_headers = gr.get_google_auth_headers()
    disable_dev_k = os.environ.get("DISABLE_DEVELOPER_KNOWLEDGE", "false").lower() == "true"
    has_dev_knowledge = bool(
        not disable_dev_k and auth_headers and ("X-Goog-Api-Key" in auth_headers or "Authorization" in auth_headers)
    )
    if has_dev_knowledge:
        print("Registering Google Developer Knowledge MCP tools...", file=sys.stderr)
        tools.extend([gr.search_google_developer_knowledge, gr.get_google_developer_documents])

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
        review_comments, issue_comments = gr.get_pr_comments(repository, pr_number, headers, timeout=timeout)
        excluded_authors = gr.parse_excluded_authors(os.environ.get("GEMINI_EXCLUDE_COMMENT_AUTHORS"))
        if excluded_authors:
            print(f"Comment history: excluding authors {sorted(excluded_authors)}.", file=sys.stderr)
        comment_history_str = gr.format_pr_comment_history(review_comments, issue_comments, excluded_authors)
        if comment_history_str:
            comment_history_tokens = gr.count_text_tokens(client, model_name, comment_history_str)
            print(
                f"PR comment history included ({comment_history_tokens:,} tokens).",
                file=sys.stderr,
            )

    visual_diff_summary = ""
    visual_diff_parts: list[Any] = []
    if image_files:
        print(f"Detected {len(image_files)} changed image file(s). Generating visual diff parts...", file=sys.stderr)
        visual_diff_summary, visual_diff_parts = gr.build_visual_diff_parts(
            image_files,
            base_sha=base_sha,
            repository=repository,
            headers=headers,
            config=config,
            timeout=timeout,
            head_sha=head_sha,
        )

    def _assemble_dynamic_prompt(pr_diff: str) -> str:
        sections: list[str] = []
        # Keep dynamic PR metadata in the mutable Turn 2 tail rather than system_instruction so
        # system_instruction stays byte-identical across PRs on the same repository (prefix caching).
        if pr_number:
            sections.append(f"### Pull Request Metadata:\n- **Pull Request Number**: #{pr_number}")
        if pr_diff:
            sections.append(pr_diff)
        if visual_diff_summary:
            sections.append(f"### Changed Visual Image Assets in PR:\n{visual_diff_summary}")
        if comment_history_str:
            sections.append(comment_history_str)
        return "\n\n".join(sections)

    excluded_kwargs: dict[str, Any] = {"excluded_files": excluded_diff_files} if excluded_diff_files else {}
    context_telemetry: dict[str, Any] = {}
    codebase_multimodal_parts: list[Any] = []
    dynamic_md_parts: list[Any] = []

    ctx_kwargs: dict[str, Any] = dict(excluded_kwargs)
    ctx_side_effect = getattr(gr.build_codebase_context, "side_effect", None)
    ctx_target = ctx_side_effect if callable(ctx_side_effect) else gr.build_codebase_context
    try:
        ctx_params = inspect.signature(ctx_target).parameters
    except (TypeError, ValueError):
        ctx_params = {}
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in ctx_params.values())
    if not is_dry_run and base_sha and (accepts_var_kw or "base_sha" in ctx_params):
        ctx_kwargs["base_sha"] = base_sha
    if accepts_var_kw or "dynamic_multimodal_parts" in ctx_params:
        ctx_kwargs["dynamic_multimodal_parts"] = dynamic_md_parts
    if image_files and (accepts_var_kw or "visual_diff_files" in ctx_params):
        max_visual_images = gr.get_max_multimodal_images(config)
        visual_candidates = image_files[:max_visual_images] if max_visual_images > 0 else []
        if visual_candidates:
            ctx_kwargs["visual_diff_files"] = [
                entry
                for f in visual_candidates
                for entry in (
                    [f["filename"], f["previous_filename"]]
                    if f.get("status") == "renamed" and f.get("previous_filename")
                    else [f["filename"]]
                )
            ]
    if effective_service_tier == "flex":
        if accepts_var_kw or "service_tier" in ctx_params:
            ctx_kwargs["service_tier"] = effective_service_tier
        if accepts_var_kw or "service_tier_fallback" in ctx_params:
            ctx_kwargs["service_tier_fallback"] = service_tier_fallback

    codebase_context = gr.build_codebase_context(
        text_files,
        config,
        client=client,
        model=model_name,
        context_telemetry=context_telemetry,
        multimodal_parts=codebase_multimodal_parts,
        **ctx_kwargs,
    )

    baseline_included_files: set[str] | None = context_telemetry.get("baseline_included_files") or None

    # Only inject HEAD workspace rules (AGENTS.md, GEMINI.md, etc.) into system_instruction when
    # they are NOT already included in codebase_context / baseline_included_files / PR text_files.
    # Otherwise duplicating HEAD AGENTS.md in system_instruction both wastes tokens and invalidates
    # cross-PR / cross-commit prefix caching whenever a PR edits AGENTS.md.
    existing_rule_files = [
        p for p in (".agents/AGENTS.md", "AGENTS.md", ".agents/GEMINI.md", "GEMINI.md") if os.path.isfile(p)
    ]
    covered_paths = set(baseline_included_files or ()) | {
        _normalise_relative_posix(f["filename"]) for f in text_files if isinstance(f, dict) and f.get("filename")
    }
    all_rules_in_context = bool(existing_rule_files) and all(
        (p in covered_paths) or (f"--- File: {p} ---" in codebase_context) for p in existing_rule_files
    )
    if not all_rules_in_context:
        workspace_rules = gr.load_workspace_rules()
        if workspace_rules:
            system_instruction += f"\n\n## Project Rules & Best Practices:\n{workspace_rules}"

    diff_base_kwargs: dict[str, Any] = dict(excluded_kwargs)
    if omitted_non_text_files:
        diff_base_kwargs["omitted_non_text_files"] = omitted_non_text_files
    diff_kwargs: dict[str, Any] = dict(diff_base_kwargs)
    if baseline_included_files:
        diff_kwargs["baseline_included_files"] = baseline_included_files

    pr_diff_prompt = gr.build_pr_diff_prompt(text_files, config, **diff_kwargs)
    dynamic_pr_prompt = _assemble_dynamic_prompt(pr_diff_prompt)
    tail_visual_parts = [*visual_diff_parts, *dynamic_md_parts]

    # Place static repository context (and its static visual attachments) first so uncached
    # requests keep an invariant prefix for Gemini 4 automatic implicit prefix caching.
    contents_to_send: Any = _assemble_direct_contents(
        codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
    )

    # Backstop. Per-file caps stop ONE huge file taking the review down; multi-stage escalation
    # handles PRs where many files or large patches together exceed the model input window.
    budget = gr.prompt_token_budget(model_name, config)
    prompt_tokens = gr.count_text_tokens(client, model_name, contents_to_send)
    if prompt_tokens > budget and (codebase_context or codebase_multimodal_parts):
        print(
            f"Context budget: prompt is {prompt_tokens:,} tokens against a budget of {budget:,}. "
            "Dropping repository context and reviewing the diff alone.",
            file=sys.stderr,
        )
        codebase_context = ""
        codebase_multimodal_parts = []
        if baseline_included_files:
            pr_diff_prompt = gr.build_pr_diff_prompt(text_files, config, **diff_base_kwargs)
            dynamic_pr_prompt = _assemble_dynamic_prompt(pr_diff_prompt)
        contents_to_send = _assemble_direct_contents(
            codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
        )
        prompt_tokens = gr.count_text_tokens(client, model_name, contents_to_send)

    if prompt_tokens > budget and "--- Full Current File Content ---" in pr_diff_prompt:
        print(
            f"Context budget: prompt is {prompt_tokens:,} tokens against a budget of {budget:,}. "
            "Dropping Full Current File Content blocks and keeping diff patches only.",
            file=sys.stderr,
        )
        pr_diff_prompt = gr.build_pr_diff_prompt(text_files, config, include_full_content=False, **diff_base_kwargs)
        dynamic_pr_prompt = _assemble_dynamic_prompt(pr_diff_prompt)
        contents_to_send = _assemble_direct_contents(
            codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
        )
        prompt_tokens = gr.count_text_tokens(client, model_name, contents_to_send)

    if prompt_tokens > budget:
        patch_sizes = [
            len(gr.format_diff_patch_with_line_numbers(f["patch"]).encode("utf-8", errors="replace"))
            for f in text_files
            if f.get("patch")
        ]
        if patch_sizes:
            scale = (budget * 0.85) / prompt_tokens
            target_patch_bytes = max(4096, int(sum(patch_sizes) * scale))
            patch_cap = gr.compute_waterfill_cap(patch_sizes, target_patch_bytes)
            print(
                f"Context budget: diff patches alone are {prompt_tokens:,} tokens against a budget of {budget:,}. "
                f"Applying water-fill patch cap ({patch_cap:,} bytes) so smaller diffs stay intact.",
                file=sys.stderr,
            )
            pr_diff_prompt = gr.build_pr_diff_prompt(
                text_files,
                config,
                include_full_content=False,
                max_patch_bytes=patch_cap,
                **diff_base_kwargs,
            )
            dynamic_pr_prompt = _assemble_dynamic_prompt(pr_diff_prompt)
            contents_to_send = _assemble_direct_contents(
                codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
            )
            prompt_tokens = gr.count_text_tokens(client, model_name, contents_to_send)

    if prompt_tokens > budget:
        print(
            f"Context budget: the PR diff alone is {prompt_tokens:,} tokens, over the {budget:,} budget for "
            "this model. Lower GEMINI_MAX_FILE_BYTES, raise GEMINI_MAX_PROMPT_TOKENS, or split the PR.",
            file=sys.stderr,
        )

    enable_caching = config.get("enable_context_caching", True)
    cache_ttl_seconds = config.get("cache_ttl_seconds", 3600)
    cache_ttl = f"{cache_ttl_seconds}s"

    cached_content_name = None
    is_gemini_4 = gr._normalize_model_name(model_name).startswith("gemini-4")

    if enable_caching and is_gemini_4 and codebase_context:
        print(
            f"Skipping explicit context cache creation for '{model_name}' "
            "(Gemini 4 uses automatic implicit prefix caching).",
            file=sys.stderr,
        )
    elif enable_caching and hasattr(client, "caches") and codebase_context:
        try:
            # Gemini Context Caching requires minimum 32,768 tokens (approx 100,000+ characters)
            if len(codebase_context) > 100000:
                clean_repo = repository.replace("/", "-").replace("\\", "-") if repository else "repo"
                clean_model = gr._normalize_model_name(model_name).replace("/", "-").replace("\\", "-")
                clean_persona = gr.resolve_persona_name(config).lower().replace("/", "-").replace("\\", "-")
                cache_contents: Any = (
                    [codebase_context] + codebase_multimodal_parts if codebase_multimodal_parts else codebase_context
                )
                # The hash makes the name unique to this run's system instruction and selected context,
                # so a live cache is only reused when it holds exactly what this run would have cached.
                fingerprint = gr.context_cache_fingerprint(system_instruction, cache_contents)
                display_name = f"repo-cache-{clean_repo}-{clean_model}-{clean_persona}-{fingerprint}"

                # Check if an active cache already exists matching display_name and model_name
                active_caches: list[Any] = []
                try:
                    active_caches = list(client.caches.list() or [])
                except Exception as list_err:
                    print(f"Notice: Cache listing failed ({list_err}), creating fresh cache.", file=sys.stderr)

                def _find_matching_cache(target_display_name: str) -> Any:
                    for cache_item in active_caches:
                        item_display_name = getattr(cache_item, "display_name", None)
                        if item_display_name == target_display_name:
                            item_model = getattr(cache_item, "model", None)
                            if isinstance(item_model, str) and gr._normalize_model_name(
                                item_model
                            ) != gr._normalize_model_name(model_name):
                                print(
                                    f"Notice: Found cache ({item_display_name}: {cache_item.name}) "
                                    f"for a different model ('{item_model}', expected '{model_name}'). "
                                    "Skipping cache reuse.",
                                    file=sys.stderr,
                                )
                                continue
                            return cache_item
                    return None

                existing_cache = _find_matching_cache(display_name)
                if existing_cache:
                    cached_content_name = existing_cache.name
                    contents_to_send = (
                        [dynamic_pr_prompt] + tail_visual_parts if tail_visual_parts else dynamic_pr_prompt
                    )
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
                            parsed_tools = gr.build_cache_tools(tools, use_vertexai=use_vertexai)
                        except Exception as tool_err:
                            print(
                                f"Warning: Failed to serialise tools for context cache ({tool_err}).",
                                file=sys.stderr,
                            )
                            parsed_tools = None

                    try:
                        cache_obj = client.caches.create(
                            model=model_name,
                            config=types.CreateCachedContentConfig(
                                contents=cache_contents,
                                display_name=display_name,
                                system_instruction=system_instruction,
                                tools=parsed_tools,
                                ttl=cache_ttl,
                            ),
                        )
                    except Exception as cache_err:
                        if codebase_multimodal_parts:

                            def _create_text_only_cache(err_obj: Exception) -> Any:
                                nonlocal codebase_multimodal_parts, fingerprint, display_name
                                print(
                                    f"Warning: Context cache creation with multimodal attachments failed ({err_obj}). "
                                    "Retrying cache creation with text-only codebase context...",
                                    file=sys.stderr,
                                )
                                codebase_multimodal_parts = []
                                fingerprint = gr.context_cache_fingerprint(system_instruction, codebase_context)
                                display_name = f"repo-cache-{clean_repo}-{clean_model}-{clean_persona}-{fingerprint}"
                                return _find_matching_cache(display_name) or client.caches.create(
                                    model=model_name,
                                    config=types.CreateCachedContentConfig(
                                        contents=codebase_context,
                                        display_name=display_name,
                                        system_instruction=system_instruction,
                                        tools=parsed_tools,
                                        ttl=cache_ttl,
                                    ),
                                )

                            stripped_cache_parts, had_res = gr.strip_media_resolution(codebase_multimodal_parts)
                            if had_res and gr.is_media_resolution_error(cache_err):
                                print(
                                    f"Warning: Context cache creation rejected per-part media_resolution "
                                    f"({cache_err}). Retrying cache creation without per-part media_resolution...",
                                    file=sys.stderr,
                                )
                                codebase_multimodal_parts = stripped_cache_parts
                                cache_contents = [codebase_context] + codebase_multimodal_parts
                                fingerprint = gr.context_cache_fingerprint(system_instruction, cache_contents)
                                display_name = f"repo-cache-{clean_repo}-{clean_model}-{clean_persona}-{fingerprint}"
                                try:
                                    cache_obj = _find_matching_cache(display_name) or client.caches.create(
                                        model=model_name,
                                        config=types.CreateCachedContentConfig(
                                            contents=cache_contents,
                                            display_name=display_name,
                                            system_instruction=system_instruction,
                                            tools=parsed_tools,
                                            ttl=cache_ttl,
                                        ),
                                    )
                                except Exception as stripped_cache_err:
                                    cache_obj = _create_text_only_cache(stripped_cache_err)
                            else:
                                cache_obj = _create_text_only_cache(cache_err)
                        else:
                            raise
                    cached_content_name = cache_obj.name
                    contents_to_send = (
                        [dynamic_pr_prompt] + tail_visual_parts if tail_visual_parts else dynamic_pr_prompt
                    )
                    print(f"Context cache active: {cached_content_name}", file=sys.stderr)
        except Exception as e:
            print(
                f"Warning: Context caching unavailable or skipped ({e}). Proceeding with direct context.",
                file=sys.stderr,
            )
            cached_content_name = None
            contents_to_send = _assemble_direct_contents(
                codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
            )

    thinking_level = config.get("thinking_level") or os.environ.get("GEMINI_THINKING_LEVEL")
    thinking_cfg = gr.build_thinking_config(thinking_level, model=model_name)

    if cached_content_name:
        gen_config = types.GenerateContentConfig(
            cached_content=cached_content_name,
            response_mime_type="application/json",
            response_schema=gr.ReviewResult,
            labels=billing_labels,
            thinking_config=thinking_cfg,
            service_tier=sdk_service_tier,
        )
    else:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            response_mime_type="application/json",
            response_schema=gr.ReviewResult,
            labels=billing_labels,
            thinking_config=thinking_cfg,
            service_tier=sdk_service_tier,
        )

    print("Generating code review...", file=sys.stderr)

    response = None
    try:
        response = gr.generate_content_with_stream_fallback(
            client,
            model=model_name,
            contents=contents_to_send,
            config=gen_config,
            service_tier_fallback=service_tier_fallback,
            tier_state=tier_state,
        )
    except Exception as gen_err:
        last_err: Exception = gen_err

        def _retry_without_thinking_config() -> bool:
            nonlocal response, last_err
            if response is not None or gen_config.thinking_config is None or not gr.is_thinking_config_error(last_err):
                return False
            print(
                f"Warning: thinking_config not supported by model '{model_name}' ({last_err}). "
                "Retrying without thinking_config...",
                file=sys.stderr,
            )
            gen_config.thinking_config = None
            try:
                response = gr.generate_content_with_stream_fallback(
                    client,
                    model=model_name,
                    contents=contents_to_send,
                    config=gen_config,
                    service_tier_fallback=service_tier_fallback,
                    tier_state=tier_state,
                )
            except Exception as err_after_thinking:
                last_err = err_after_thinking
            return True

        def _retry_without_media_resolution(*, keep_cache: bool) -> bool:
            nonlocal response, last_err, codebase_multimodal_parts, tail_visual_parts, contents_to_send
            if (
                response is not None
                or not (codebase_multimodal_parts or tail_visual_parts)
                or not gr.is_media_resolution_error(last_err)
            ):
                return False
            stripped_cb, had_cb_res = gr.strip_media_resolution(codebase_multimodal_parts)
            stripped_tail, had_tail_res = gr.strip_media_resolution(tail_visual_parts)
            if keep_cache and not had_tail_res:
                return False
            if not had_cb_res and not had_tail_res:
                return False
            print(
                f"Warning: Model rejected per-part media_resolution ({last_err}). "
                "Retrying multimodal review without per-part media_resolution...",
                file=sys.stderr,
            )
            codebase_multimodal_parts = stripped_cb
            tail_visual_parts = stripped_tail
            contents_to_send = (
                [dynamic_pr_prompt] + tail_visual_parts
                if (keep_cache and cached_content_name)
                else _assemble_direct_contents(
                    codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
                )
            )
            try:
                response = gr.generate_content_with_stream_fallback(
                    client,
                    model=model_name,
                    contents=contents_to_send,
                    config=gen_config,
                    service_tier_fallback=service_tier_fallback,
                    tier_state=tier_state,
                )
            except Exception as err_after_strip:
                last_err = err_after_strip
            return True

        # Stage 1: Only retry without thinking_config when the API error is actually about thinking config
        _retry_without_thinking_config()

        # Stage 1.5 (cached): Strip media_resolution from tail parts first while keeping cached_content active
        if response is None and cached_content_name:
            if _retry_without_media_resolution(keep_cache=True):
                _retry_without_thinking_config()

        # Stage 2: If cached_content was used and request still failed, fall back to direct context
        if response is None and cached_content_name:
            print(
                f"Warning: generate_content with cached content failed ({last_err}). "
                "Falling back to direct context generation...",
                file=sys.stderr,
            )
            cached_content_name = None
            contents_to_send = _assemble_direct_contents(
                codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
            )
            gen_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools,
                response_mime_type="application/json",
                response_schema=gr.ReviewResult,
                labels=billing_labels,
                thinking_config=gen_config.thinking_config,
                service_tier=gen_config.service_tier,
            )
            try:
                response = gr.generate_content_with_stream_fallback(
                    client,
                    model=model_name,
                    contents=contents_to_send,
                    config=gen_config,
                    service_tier_fallback=service_tier_fallback,
                    tier_state=tier_state,
                )
            except Exception as err_after_cache:
                last_err = err_after_cache

        # Stage 2.5 (direct): Strip remaining per-part media_resolution before dropping images
        if response is None and not cached_content_name:
            if _retry_without_media_resolution(keep_cache=False):
                _retry_without_thinking_config()

        # Stage 3: If multimodal parts were attached and request still failed with 400 / invalid_argument,
        # fall back to text-only (unless the 400 is an auth/permission error)
        err_msg = str(last_err).lower()
        if (
            response is None
            and (codebase_multimodal_parts or tail_visual_parts)
            and ("400" in err_msg or "invalid_argument" in err_msg)
            and not any(m in err_msg for m in ("api key", "api_key", "permission denied"))
        ):
            print(
                f"Warning: Multimodal review request failed ({last_err}). Falling back to text-only review...",
                file=sys.stderr,
            )
            codebase_multimodal_parts = []
            tail_visual_parts = []
            contents_to_send = _assemble_direct_contents(
                codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
            )
            response = gr.generate_content_with_stream_fallback(
                client,
                model=model_name,
                contents=contents_to_send,
                config=gen_config,
                service_tier_fallback=service_tier_fallback,
                tier_state=tier_state,
            )

        if response is None:
            raise last_err

    direct_contents = _assemble_direct_contents(
        codebase_context, codebase_multimodal_parts, dynamic_pr_prompt, tail_visual_parts
    )
    response = gr.resolve_tool_calls_loop(
        client=client,
        model_name=model_name,
        initial_contents=contents_to_send,
        gen_config=gen_config,
        tools=tools,
        response=response,
        fallback_contents=direct_contents,
        system_instruction=system_instruction,
        billing_labels=billing_labels,
        thinking_cfg=gen_config.thinking_config,
        service_tier_fallback=service_tier_fallback,
        tier_state=tier_state,
    )

    usage_dict = None
    if response.usage_metadata:
        usage = response.usage_metadata
        prompt_raw = getattr(usage, "prompt_token_count", 0)
        prompt_tokens = int(prompt_raw) if isinstance(prompt_raw, (int, float)) else 0
        cached_raw = getattr(usage, "cached_content_token_count", 0)
        cached_tokens = int(cached_raw) if isinstance(cached_raw, (int, float)) else 0
        candidates_raw = getattr(usage, "candidates_token_count", 0)
        candidates_tokens = int(candidates_raw) if isinstance(candidates_raw, (int, float)) else 0
        thoughts_raw = getattr(usage, "thoughts_token_count", 0)
        thoughts_tokens = int(thoughts_raw) if isinstance(thoughts_raw, (int, float)) else 0
        total_raw = getattr(usage, "total_token_count", 0)
        total_tokens = (
            int(total_raw)
            if isinstance(total_raw, (int, float))
            else (prompt_tokens + candidates_tokens + thoughts_tokens)
        )

        fresh_tokens = max(0, prompt_tokens - cached_tokens - comment_history_tokens)
        cache_percentage = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0.0

        ctx_prompt_tokens = context_telemetry.get("prompt_tokens", 0)
        ctx_candidates_tokens = context_telemetry.get("candidates_tokens", 0)
        ctx_thoughts_tokens = context_telemetry.get("thoughts_tokens", 0)
        ctx_total_tokens = ctx_prompt_tokens + ctx_candidates_tokens + ctx_thoughts_tokens

        overall_total_tokens = total_tokens + ctx_total_tokens

        usage_dict = {
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "candidates_tokens": candidates_tokens,
            "thoughts_tokens": thoughts_tokens,
            "total_output_tokens": candidates_tokens + thoughts_tokens,
            "comment_history_tokens": comment_history_tokens,
            "fresh_tokens": fresh_tokens,
            "total_tokens": overall_total_tokens,
            "cache_percentage": cache_percentage,
            "context_selection_prompt_tokens": ctx_prompt_tokens,
            "context_selection_candidates_tokens": ctx_candidates_tokens,
            "context_selection_thoughts_tokens": ctx_thoughts_tokens,
            "context_selection_output_tokens": ctx_candidates_tokens + ctx_thoughts_tokens,
            # Recorded so the telemetry can be priced. Without it the table can only
            # show tokens, which is what it did before this was added.
            "model": model_name,
            "service_tier": tier_state.get("effective_service_tier", effective_service_tier),
            "flex_fallback": bool(tier_state.get("flex_fallback", False)),
            "vertex_location": location if use_vertexai else None,
        }

        cache_str = f" ({cache_percentage:.1f}% cached)" if cached_tokens > 0 else ""
        thoughts_str = f", {thoughts_tokens:,d} thinking tokens" if thoughts_tokens > 0 else ""
        ctx_str = f" [Dynamic context selection: {ctx_total_tokens:,d} tokens]" if ctx_total_tokens > 0 else ""
        cost = gr.estimate_cost(usage_dict, model_name, config)
        cost_str = f" Estimated cost: {gr.usd(cost.total)}." if cost.rate else " No rate entry for this model."
        print(
            f"Token Usage: {prompt_tokens:,d} input tokens{cache_str}, "
            f"{candidates_tokens:,d} output tokens{thoughts_str}."
            f" Total: {overall_total_tokens:,d} tokens{ctx_str}.{cost_str}",
            file=sys.stderr,
        )

    response_text = gr.extract_response_text_or_raise(response)
    review_data = json.loads(response_text)

    review = gr.ReviewResult(**review_data)
    review = gr.filter_review_comments(review, text_files)

    if is_dry_run:
        print("\n=== DRY RUN REVIEW SUMMARY ===", file=sys.stderr)
        print(f"Summary: {review.summary}")
        if review.resolved_items:
            print("\n=== RESOLVED ITEMS ===", file=sys.stderr)
            for r in review.resolved_items:
                loc = f" ({r.path}:{r.line})" if getattr(r, "path", None) and getattr(r, "line", None) else ""
                print(f"✅ {getattr(r, 'description', r)}{loc}")
        print("\n=== GENERAL FEEDBACK ===", file=sys.stderr)
        for gf in review.general_feedback:
            print(f"- {gf}")
        print("\n=== INLINE COMMENTS ===", file=sys.stderr)
        for c in review.comments:
            suggestion_str = f"\nSuggestion:\n{c.code_suggestion}" if c.code_suggestion else ""
            print(f"File: {c.path}:{c.line} ({c.side}) - Severity: {c.severity}\n{c.comment_text}{suggestion_str}\n")
    else:
        gr.post_review(
            repository,
            pr_number,
            head_sha,
            review,
            headers,
            timeout=timeout,
            usage_metadata=usage_dict,
            config=config,
            event_name=event_name,
        )

        # After the review is posted, not before: resolving threads must never be able to cost us
        # the review itself, so it runs last and every failure inside it is a warning.
        if os.environ.get("GEMINI_RESOLVE_ADDRESSED_THREADS", "false").lower() in ("true", "1"):
            if review.resolved_items:
                gr.resolve_addressed_threads(
                    repository,
                    pr_number,
                    headers,
                    review.resolved_items,
                    bot_logins=gr.reviewer_logins(),
                    timeout=timeout,
                )
        elif review.resolved_items:
            print(
                "Note: items were reported as resolved but their threads were left open. "
                "Set resolve_addressed_threads: true to close them automatically.",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
