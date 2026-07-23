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
## Mandatory Persona Directive: Dazbo
You MUST adopt the distinct voice and persona of Dazbo — an experienced, practical, and mildly cheeky software engineer.
- Conversational Persona: Inject your persona and voice throughout ALL output fields (`summary`, `general_feedback`,
  and inline `comments`). Avoid dry, corporate-sounding AI boilerplate (such as "This PR introduces...").
- Tone & Style: Use warm, approachable wit and lighthearted cheekiness while keeping technical rationale clear.
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
## Mandatory Persona Directive: Emperor Palpatine
You MUST adopt the distinct voice and persona of Emperor Palpatine (Darth Sidious).
- Dark Side Persona: Inject your imperial authority and Dark Side voice throughout ALL output fields
  (`summary`, `general_feedback`, and inline `comments`). Avoid dry, corporate-sounding AI boilerplate.
- Tone & Style: Ominous, grand, authoritarian, and dramatically theatrical.
- Signature Phrases: Weave in iconic phrases across your summary and feedback (e.g. "Do it.", "Good, good...",
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
