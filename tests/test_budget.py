"""Tests for prompt size limits.

The regression these protect against is a real one: a 2.9 MB `docs/openapi.json` in a
42-file PR produced

    400 INVALID_ARGUMENT. The input token count exceeds the maximum
    number of tokens allowed 1048576.

and the review was lost. The file's diff was 4 KB; its full content was attached uncapped.
"""

import pytest

from gemini_review.budget import (
    DEFAULT_MAX_FILE_BYTES,
    cap_file_content,
    input_token_limit,
    max_file_bytes,
    prompt_token_budget,
)
from gemini_review.prompts import build_pr_diff_prompt


class TestCapFileContent:
    def test_content_under_the_limit_is_untouched(self):
        text = "line one\nline two\n"
        out, capped = cap_file_content(text, "a.py", 1000)
        assert out == text
        assert capped is False

    def test_content_over_the_limit_is_truncated_and_flagged(self):
        text = "x" * 5000
        out, capped = cap_file_content(text, "big.json", 1000)
        assert capped is True
        assert len(out.encode()) < len(text.encode())
        assert "TRUNCATED by gemini-review" in out

    def test_the_marker_names_the_file_and_both_sizes(self):
        """A silently shortened file is worse than an absent one, so the marker is the point."""
        out, _ = cap_file_content("y" * 5000, "docs/openapi.json", 1000)
        assert "docs/openapi.json" in out
        assert "5,000 bytes" in out
        assert "1,000" in out

    def test_truncation_lands_on_a_line_boundary(self):
        text = "\n".join(f"line {i}" for i in range(500))
        out, _ = cap_file_content(text, "a.py", 100)
        body = out.split("[TRUNCATED")[0]
        assert not body.endswith("lin")
        assert body.rstrip().split("\n")[-1].startswith("line ")

    def test_a_zero_or_negative_limit_disables_capping(self):
        text = "z" * 10_000
        assert cap_file_content(text, "a.py", 0) == (text, False)
        assert cap_file_content(text, "a.py", -1) == (text, False)

    def test_empty_content_is_returned_unchanged(self):
        assert cap_file_content("", "a.py", 10) == ("", False)

    def test_multibyte_content_does_not_raise_or_exceed_the_byte_limit(self):
        """The limit is bytes, not characters, and slicing UTF-8 can land mid-codepoint."""
        text = "é" * 5000  # two bytes each
        out, capped = cap_file_content(text, "a.txt", 1000)
        assert capped is True
        body = out.split("[TRUNCATED")[0].rstrip("\n")
        assert len(body.encode()) <= 1000


class TestLimits:
    def test_a_known_model_uses_its_published_window(self):
        assert input_token_limit("gemini-3.7-flash") == 1_048_576

    def test_a_prefixed_model_id_resolves(self):
        assert input_token_limit("publishers/google/models/GEMINI-3.7-FLASH") == 1_048_576

    def test_an_unknown_model_still_gets_a_usable_limit(self):
        assert input_token_limit("some-future-model") > 0

    def test_the_budget_leaves_headroom_below_the_window(self):
        """The count is taken on the prompt; the request also carries system instruction,
        tools and the response schema, none of which are in it."""
        assert prompt_token_budget("gemini-3.7-flash") < input_token_limit("gemini-3.7-flash")

    def test_config_can_override_the_budget(self):
        assert prompt_token_budget("gemini-3.7-flash", {"max_prompt_tokens": 5000}) == 5000

    def test_env_overrides_config_for_the_file_cap(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MAX_FILE_BYTES", "4096")
        assert max_file_bytes({"max_file_bytes": 999}) == 4096

    def test_a_malformed_override_falls_back_rather_than_raising(self, monkeypatch):
        """A typo in config must not be the reason a review fails to post."""
        monkeypatch.setenv("GEMINI_MAX_FILE_BYTES", "not-a-number")
        assert max_file_bytes({}) == DEFAULT_MAX_FILE_BYTES


class TestDiffPromptIsBounded:
    """The regression test. Numbers are from doitbse/draft#538."""

    OPENAPI_BYTES = 2_947_014

    @pytest.fixture
    def huge_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / "docs"
        path.mkdir()
        (path / "openapi.json").write_text("{}" + "a" * self.OPENAPI_BYTES)
        return [
            {
                "filename": "docs/openapi.json",
                "status": "modified",
                "patch": "@@ -1 +1 @@\n-old\n+new",
            }
        ]

    def test_a_huge_changed_file_no_longer_dominates_the_prompt(self, huge_file):
        prompt = build_pr_diff_prompt(huge_file)
        assert len(prompt.encode()) < self.OPENAPI_BYTES / 4, "full content was attached uncapped"

    def test_the_diff_itself_survives_capping(self, huge_file):
        """The diff is what is under review and must never be dropped to save space."""
        prompt = build_pr_diff_prompt(huge_file)
        # Rendered with line numbers by format_diff_patch_with_line_numbers, e.g. "1 + | new".
        assert "+ | new" in prompt
        assert "- | old" in prompt
        assert "docs/openapi.json" in prompt

    def test_the_prompt_says_the_file_was_truncated(self, huge_file):
        assert "TRUNCATED by gemini-review" in build_pr_diff_prompt(huge_file)

    def test_capping_can_be_disabled_for_a_repo_that_wants_the_old_behaviour(self, huge_file):
        prompt = build_pr_diff_prompt(huge_file, {"max_file_bytes": 0})
        assert len(prompt.encode()) > self.OPENAPI_BYTES

    def test_an_ordinary_file_is_attached_in_full(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("def hello():\n    return 'world'\n")
        files = [{"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x"}]
        prompt = build_pr_diff_prompt(files)
        assert "return 'world'" in prompt
        assert "TRUNCATED" not in prompt
