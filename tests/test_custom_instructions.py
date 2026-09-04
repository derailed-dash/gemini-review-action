"""
Description: Unit tests for custom instructions and guardrails loader.
Verifies loading from default and fallback file locations, inline text,
path traversal security, graceful non-existence handling, and prompt injection.
"""

from pathlib import Path

import pytest

from gemini_review.prompts import load_custom_instructions, load_system_instruction


class TestCustomInstructions:
    def test_load_custom_instructions_from_default_github_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify reading from default .github/review-instruction-additions.md."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GEMINI_CUSTOM_INSTRUCTIONS", raising=False)

        github_dir = tmp_path / ".github"
        github_dir.mkdir()
        instructions_file = github_dir / "review-instruction-additions.md"
        instructions_file.write_text("Guardrail: Check all inputs for SQL injection.", encoding="utf-8")

        content = load_custom_instructions()
        assert content == "Guardrail: Check all inputs for SQL injection."

    def test_load_custom_instructions_from_root_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify fallback to review-instruction-additions.md at repository root."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GEMINI_CUSTOM_INSTRUCTIONS", raising=False)

        root_file = tmp_path / "review-instruction-additions.md"
        root_file.write_text("Guardrail: Enforce English (UK) spelling.", encoding="utf-8")

        content = load_custom_instructions()
        assert content == "Guardrail: Enforce English (UK) spelling."

    def test_load_custom_instructions_default_nonexistent_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When default file does not exist, silently return empty string."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GEMINI_CUSTOM_INSTRUCTIONS", raising=False)

        content = load_custom_instructions()
        assert content == ""

    def test_load_custom_instructions_explicit_file_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify loading an explicit file path specified via argument or env var."""
        monkeypatch.chdir(tmp_path)
        custom_file = tmp_path / "custom-guardrails.md"
        custom_file.write_text("Rule: No dependencies without ADR.", encoding="utf-8")

        content = load_custom_instructions("custom-guardrails.md")
        assert content == "Rule: No dependencies without ADR."

        monkeypatch.setenv("GEMINI_CUSTOM_INSTRUCTIONS", "custom-guardrails.md")
        assert load_custom_instructions() == "Rule: No dependencies without ADR."

    def test_load_custom_instructions_inline_text(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify that raw text instructions (not a file path) are returned directly."""
        monkeypatch.chdir(tmp_path)
        raw_text = "- Must check for memory leaks.\n- Ensure tests pass."

        content = load_custom_instructions(raw_text)
        assert content == raw_text

    def test_load_custom_instructions_single_line_with_slashes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify that single-line prose containing forward slashes is not treated as a file path."""
        monkeypatch.chdir(tmp_path)
        prose = "Ensure all /api endpoints validate query parameters and client/server models"

        content = load_custom_instructions(prose)
        assert content == prose

    def test_load_custom_instructions_nonexistent_file_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Verify that a path-like string that does not exist returns an empty string."""
        monkeypatch.chdir(tmp_path)
        content = load_custom_instructions("missing_dir/custom_rules.md")
        assert content == ""

    def test_load_custom_instructions_path_traversal_blocked(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verify path traversal outside workspace is blocked."""
        monkeypatch.chdir(tmp_path)
        content = load_custom_instructions("../../../etc/passwd")
        assert content == ""

    def test_load_system_instruction_substitutes_additional_context(self):
        """When TOML prompt contains !{echo $ADDITIONAL_CONTEXT}, replace it with custom instructions."""
        config = {"prompt": "Base instructions.\n- **Additional User Instructions**: !{echo $ADDITIONAL_CONTEXT}\nEnd."}
        instructions = "Custom guardrails: verify all endpoints."
        result = load_system_instruction(
            repository="owner/repo",
            pr_number=42,
            config=config,
            custom_instructions=instructions,
        )
        assert "Custom guardrails: verify all endpoints." in result
        assert "!{echo $ADDITIONAL_CONTEXT}" not in result

    def test_load_system_instruction_appends_when_placeholder_missing(self):
        """When TOML prompt has no placeholder, append custom instructions under dedicated section."""
        config = {"prompt": "Custom prompt without placeholders."}
        instructions = "Enforce PEP 8 style guide."
        result = load_system_instruction(
            repository="owner/repo",
            pr_number=42,
            config=config,
            custom_instructions=instructions,
        )
        assert "Custom prompt without placeholders." in result
        assert "## Additional Review Instructions & Guardrails:" in result
        assert "Enforce PEP 8 style guide." in result

    def test_load_system_instruction_fallback_prompt_appends_instructions(self):
        """When using default fallback prompt (no config prompt), append custom instructions."""
        config = {}
        instructions = "Ensure all API calls have timeouts."
        result = load_system_instruction(
            repository="owner/repo",
            pr_number=42,
            config=config,
            custom_instructions=instructions,
        )
        assert "You are a world-class software engineering and technical review agent" in result
        assert "## Additional Review Instructions & Guardrails:" in result
        assert "Ensure all API calls have timeouts." in result
