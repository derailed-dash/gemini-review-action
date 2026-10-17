"""
Unit tests verifying that the bundled installation skill (.agents/skills/install-gemini-code-review-action/SKILL.md)
stays synchronised with the action inputs in action.yml and reviewer personas in gemini_review/personas.py.

Why it exists:
Prevents documentation and skill drift when new action inputs or reviewer personas are added to
gemini-review-action, reminding maintainers to update both the local SKILL.md and the canonical
dazbo-agent-skills repository.
"""

from __future__ import annotations

import re
from pathlib import Path

from gemini_review.personas import (
    PERSONA_DAZBO,
    PERSONA_PALPATINE,
    PERSONA_RICK,
    PERSONA_STRAIGHT,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD_PATH = REPO_ROOT / ".agents" / "skills" / "install-gemini-code-review-action" / "SKILL.md"
ACTION_YML_PATH = REPO_ROOT / "action.yml"


def _parse_action_yml_inputs(action_yml_text: str) -> list[str]:
    """Extract top-level input names under the 'inputs:' section of action.yml."""
    inputs: list[str] = []
    in_inputs = False
    for line in action_yml_text.splitlines():
        if re.match(r"^inputs:\s*$", line):
            in_inputs = True
            continue
        if in_inputs:
            # Stop when another top-level YAML key starts
            if re.match(r"^[a-zA-Z0-9_-]+:\s*", line):
                break
            match = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
            if match:
                inputs.append(match.group(1))
    return inputs


def test_install_skill_exists_and_has_frontmatter() -> None:
    """Verify the installation skill exists in .agents/skills/ with valid frontmatter."""
    assert SKILL_MD_PATH.is_file(), f"Expected skill file at {SKILL_MD_PATH}"
    content = SKILL_MD_PATH.read_text(encoding="utf-8")
    assert content.startswith("---\nname: install-gemini-code-review-action\n")


def test_install_skill_documents_all_action_inputs() -> None:
    """Verify every input defined in action.yml is documented in SKILL.md."""
    action_inputs = _parse_action_yml_inputs(ACTION_YML_PATH.read_text(encoding="utf-8"))
    assert len(action_inputs) > 0, "Failed to parse any inputs from action.yml"

    skill_content = SKILL_MD_PATH.read_text(encoding="utf-8")
    missing_inputs = [inp for inp in action_inputs if f"`{inp}`" not in skill_content]
    assert not missing_inputs, (
        f"The following action.yml inputs are missing from {SKILL_MD_PATH.relative_to(REPO_ROOT)}: "
        f"{missing_inputs}. Please update SKILL.md (and sync to derailed-dash/dazbo-agent-skills)."
    )


def test_install_skill_documents_all_personas() -> None:
    """Verify all supported reviewer personas are documented in SKILL.md."""
    skill_content = SKILL_MD_PATH.read_text(encoding="utf-8")
    expected_personas = [
        PERSONA_STRAIGHT,
        PERSONA_DAZBO,
        PERSONA_PALPATINE,
        PERSONA_RICK,
    ]
    missing_personas = [p for p in expected_personas if f"`{p}`" not in skill_content]
    assert not missing_personas, (
        f"The following personas are missing from {SKILL_MD_PATH.relative_to(REPO_ROOT)}: "
        f"{missing_personas}. Please update SKILL.md (and sync to derailed-dash/dazbo-agent-skills)."
    )
