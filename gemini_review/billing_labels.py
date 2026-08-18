"""
Description: Billing labels for cost attribution on Vertex AI.

Answers "what do code reviews cost us on this repository" from the billing data itself,
rather than by adding up numbers in review comments. Vertex attaches request labels to the
billed charge, so they arrive in the Cloud Billing export and cost becomes a group-by.

**Vertex only, and that is a property of the API rather than a choice made here.** The
Gemini Developer API's GenerateContentRequest has no `labels` field at all (its documented
fields are contents, tools, toolConfig, safetySettings, systemInstruction, generationConfig,
cachedContent, serviceTier and store), and the SDK raises rather than sending one:

    ValueError: labels parameter is only supported in Gemini Enterprise Agent Platform
    mode, not in Gemini Developer API mode.

So `build_labels` returns None off Vertex and the caller omits the field. Users
authenticating with an API key see one notice line and lose nothing they had before.
"""

import os
import re
import sys
from typing import Any

# GCP label syntax: keys and values may hold lowercase letters, digits, hyphens and
# underscores, up to 63 characters, and a key must begin with a lowercase letter. A repo
# name like "owner/name" is therefore not a legal value until the slash is replaced, which
# is the single most likely thing to be passed in here.
MAX_LABEL_LENGTH = 63
_ILLEGAL = re.compile(r"[^a-z0-9_-]")

DISABLE_VALUES = frozenset({"none", "off", "false", "0"})


def sanitise(value: str) -> str:
    """Coerce a string into something GCP will accept as a label value."""
    cleaned = _ILLEGAL.sub("_", value.strip().lower())[:MAX_LABEL_LENGTH]
    return cleaned.strip("_-")


def sanitise_key(key: str) -> str:
    """Same, plus the rule that a key must start with a lowercase letter."""
    cleaned = sanitise(key)
    if cleaned and not cleaned[0].isalpha():
        cleaned = f"k_{cleaned}"[:MAX_LABEL_LENGTH]
    return cleaned


def parse_pairs(raw: str) -> dict[str, str]:
    """Parse `k=v,k=v` into a sanitised dict, skipping anything malformed.

    Skipping rather than raising is deliberate: a typo in an optional label should not fail
    a review that has already been paid for.
    """
    labels: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            if chunk.strip():
                print(f"Notice: ignoring malformed label '{chunk.strip()}' (expected key=value).", file=sys.stderr)
            continue
        key, _, value = chunk.partition("=")
        key, value = sanitise_key(key), sanitise(value)
        if key and value:
            labels[key] = value
    return labels


def build_labels(client: Any, config: dict | None = None, repository: str | None = None) -> dict[str, str] | None:
    """The labels to attach to this run's requests, or None when they cannot be used.

    None means "omit the field", which is the correct behaviour off Vertex and when the user
    has switched the feature off.
    """
    config = config or {}
    raw = os.environ.get("GEMINI_BILLING_LABELS", config.get("billing_labels"))

    if raw is not None and str(raw).strip().lower() in DISABLE_VALUES:
        return None

    if not getattr(client, "vertexai", False):
        # Only worth saying when the user asked for labels; otherwise it is noise on the
        # default path.
        if raw:
            print(
                "Notice: billing labels are a Vertex AI feature and are ignored when "
                "authenticating with a Gemini API key. Set GOOGLE_GENAI_USE_VERTEXAI to use them.",
                file=sys.stderr,
            )
        return None

    repo = repository or os.environ.get("GITHUB_REPOSITORY", "")
    labels = {"component": "gemini-review-action"}
    if repo:
        # "owner/name" is not a legal label value, so the slash becomes an underscore.
        labels["repo"] = sanitise(repo)

    # Deliberately NOT the PR number. It would make every pull request its own dimension in
    # the billing export, which is a lot of noise for a question ("what do reviews cost on
    # this repo") that is answered at repo level. Anyone who wants finer grain can add it.
    if raw:
        labels.update(parse_pairs(str(raw)))

    labels = {k: v for k, v in labels.items() if k and v}
    return labels or None
