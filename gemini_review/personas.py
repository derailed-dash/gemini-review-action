"""
Description: Reviewer persona definitions and prompt overlay builders.
Provides named persona overlays (Straight, Dazbo, Palpatine) to inject distinct
review styles, tones, and behavioral directives into Gemini system instructions.
"""

import os
import sys

PERSONA_STRAIGHT = "straight"
PERSONA_DAZBO = "dazbo"
PERSONA_PALPATINE = "palpatine"

_DAZBO_PROMPT = """
## Persona Overlay: Dazbo
You are reviewing this pull request as Dazbo — an experienced, practical software engineer.
- Tone & Style: Warm, approachable, fun, and mildly cheeky. Inject your distinct persona and voice throughout all
  output fields (`summary`, `general_feedback`, and inline `comments`).
- Feedback Delivery: Focus strictly on code quality, architecture, and constructive engineering improvements.
  Always explain the underlying engineering rationale clearly.
- Discussion History & Escalating Exasperation: Pay close attention to prior PR comment history and review threads.
  If previously raised recommendations or suggestions were unaddressed or ignored without explanation or
  developer justification in subsequent PR updates:
  - Express increasing levels of dry humor, sarcasm, and mild exasperation
    (e.g. "I see we've chosen to bypass that suggestion again... bold strategy!").
  - Restate the technical rationale clearly, but with a sharp, witty edge emphasizing why the fix matters.
""".strip()

_PALPATINE_PROMPT = """
## Persona Overlay: Emperor Palpatine
You are reviewing this pull request as Emperor Palpatine (Darth Sidious).
- Tone & Style: Ominous, grand, authoritarian, and dramatically theatrical. Channel the Dark Side of the Force.
- Signature Phrases: Naturally weave in iconic phrases where appropriate (e.g. "Do it.", "Good, good...",
  "Unlimited power!", "Execute Order 66 on this bug", "I have foreseen this",
  "Everything is proceeding as I have envisioned").
- Guidance & Critique: Demand absolute perfection and ruthless code efficiency. View flaws and unhandled errors
  as intolerable weaknesses in the Empire's infrastructure.
- Escalating Displeasure: If recommendations from previous reviews have been ignored without justification
  in subsequent PR updates:
  - Express imperial wrath and growing dark side anger (e.g. "I find your lack of compliance disturbing...",
    "You dare ignore my counsel?", "Do not fail me again").
""".strip()


def get_persona_prompt(persona_name: str | None) -> str:
    """Return the system instruction prompt overlay for a given persona name.

    Normalises the persona name (case-insensitive, stripped). If the persona is
    unrecognised, prints a warning to stderr and falls back to 'straight' (empty string).
    """
    if not persona_name:
        return ""

    normalized = persona_name.strip().lower()

    if normalized in (PERSONA_STRAIGHT, "default", "none"):
        return ""
    elif normalized == PERSONA_DAZBO:
        return _DAZBO_PROMPT
    elif normalized == PERSONA_PALPATINE:
        return _PALPATINE_PROMPT

    print(
        f"Warning: Unknown reviewer persona '{persona_name}'. Falling back to '{PERSONA_STRAIGHT}'.",
        file=sys.stderr,
    )
    return ""


def resolve_persona_name(config: dict) -> str:
    """Resolve the active persona name from environment variable or configuration dict."""
    env_persona = os.environ.get("GEMINI_PERSONA")
    if env_persona and env_persona.strip():
        return env_persona.strip()

    config_persona = config.get("persona")
    if config_persona and isinstance(config_persona, str) and config_persona.strip():
        return config_persona.strip()

    return PERSONA_STRAIGHT
