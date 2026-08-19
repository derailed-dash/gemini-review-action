"""
Description: Size limits for anything attached to the review prompt.

The failure this exists to prevent: a single generated file in a PR blows the model's
input window and the whole review dies with a 400, posting nothing.

    400 INVALID_ARGUMENT. The input token count exceeds the maximum
    number of tokens allowed 1048576.

That was one 2.9 MB `docs/openapi.json` in a 42-file PR, roughly 980k tokens on its own.
Its *diff* was 4 KB. The action attached the full current content of every changed text
file with no size limit, so the review was lost to a file nobody was reviewing.

Two rules, because they fail differently:

1. **Per-file cap, applied at every point where full file content is attached.** Cheap,
   deterministic, needs no API call. This is the one that actually prevents the failure.
2. **A preflight total, checked once before the expensive call.** A backstop for the case
   the per-file cap cannot catch: many files each under the cap that together exceed the
   window.

Why a cap and not a longer exclusion list: the existing guard is a set of filenames
(`package-lock.json`, `uv.lock`, `.env`). `package-lock.json` is on it because someone
thought of it; a generated OpenAPI spec is not, because nobody did. Enumerating bad names
fails on the next generated file, and every repo has a different one — a snapshot, a
bundled schema, a fixture, a vendored client. A byte cap catches all of them, including
the ones not invented yet.

Truncation is deliberately loud. A silently shortened file is worse than an absent one,
because the model reasons confidently about content it cannot see and nobody reading the
review can tell.
"""

import os
import sys

# Per-file ceiling on ATTACHED FULL CONTENT. Diffs are never capped by this: the diff is
# the thing under review, and diffs are small even for enormous files.
#
# 128 KB is about 32k tokens, so a handful of capped files still leaves room in a 1M
# window. It is comfortably above real source files — the largest hand-written file in the
# repos this was tested against is under 60 KB — so in practice this only ever bites
# generated artifacts, which is the intent.
DEFAULT_MAX_FILE_BYTES = 128 * 1024

# Published input windows. Used only to pick a preflight budget; an unknown model falls
# back to the smallest known value rather than assuming the largest, so a new model id
# fails safe (a trimmed review) instead of unsafe (a 400 and no review).
MODEL_INPUT_TOKEN_LIMITS: dict[str, int] = {
    "gemini-3.7-flash": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
}
_FALLBACK_INPUT_TOKEN_LIMIT = 1_048_576

# Leave headroom. The count is taken on the prompt text, but the request also carries the
# system instruction, tool declarations and the response schema, none of which are in it.
DEFAULT_PROMPT_TOKEN_HEADROOM = 0.90


def _normalise_model(model: str | None) -> str:
    if not model:
        return ""
    name = model.strip().lower()
    if "models/" in name:
        name = name.split("models/")[-1]
    return name


def input_token_limit(model: str | None) -> int:
    """The input window for `model`, defaulting to the smallest known limit."""
    return MODEL_INPUT_TOKEN_LIMITS.get(_normalise_model(model), _FALLBACK_INPUT_TOKEN_LIMIT)


def prompt_token_budget(model: str | None, config: dict | None = None) -> int:
    """How many tokens the assembled prompt may occupy."""
    config = config or {}
    explicit = _int_setting("GEMINI_MAX_PROMPT_TOKENS", config, "max_prompt_tokens", 0)
    if explicit > 0:
        return explicit
    return int(input_token_limit(model) * DEFAULT_PROMPT_TOKEN_HEADROOM)


def _int_setting(env_var: str, config: dict, config_key: str, default: int) -> int:
    """An int from the environment, else gemini-review.toml, else the default.

    A malformed value falls back rather than raising. This is a guard rail: a typo in a
    config file should not be the reason a review fails to post.
    """
    if env_var in os.environ:
        try:
            return int(os.environ[env_var])
        except ValueError:
            print(
                f"Warning: {env_var}='{os.environ[env_var]}' is not an integer. Using the configured default.",
                file=sys.stderr,
            )
    value = (config or {}).get(config_key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def max_file_bytes(config: dict | None = None) -> int:
    """Per-file ceiling on attached full content. 0 or negative disables the cap."""
    return _int_setting("GEMINI_MAX_FILE_BYTES", config or {}, "max_file_bytes", DEFAULT_MAX_FILE_BYTES)


def cap_file_content(content: str, filename: str, limit: int) -> tuple[str, bool]:
    """Return `content` truncated to `limit` bytes, and whether it was truncated.

    Truncation is on a line boundary so the retained portion stays parseable, and carries
    a marker naming the file and both sizes. The marker is the point: it tells the model
    the file continues, and it tells a human reading the review why a finding might be
    partial.
    """
    if not content or limit <= 0:
        return content, False

    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return content, False

    kept = encoded[:limit].decode("utf-8", errors="ignore")
    # Drop the final, probably half-written line.
    newline = kept.rfind("\n")
    if newline > 0:
        kept = kept[:newline]

    marker = (
        f"\n\n[TRUNCATED by gemini-review: '{filename}' is {len(encoded):,} bytes, "
        f"showing the first {limit:,}. The rest of this file was NOT provided. "
        f"Do not draw conclusions about the omitted portion.]"
    )
    return kept + marker, True


def report_capped(filenames: list[str], limit: int) -> None:
    """Say once, on stderr, which files were shortened. Silence here is the real hazard."""
    if not filenames:
        return
    print(
        f"Context budget: truncated {len(filenames)} file(s) to {limit:,} bytes of full content "
        f"(diffs are unaffected): {', '.join(filenames)}",
        file=sys.stderr,
    )
