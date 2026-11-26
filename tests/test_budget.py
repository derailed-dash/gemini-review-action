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
        assert input_token_limit("gemini-4-hydrogen") == 1_048_576
        assert input_token_limit("gemini-4-neon") == 1_048_576
        assert input_token_limit("gemini-4-argon") == 1_048_576
        assert input_token_limit("gemini-4-carbon") == 1_048_576
        assert input_token_limit("gemini-decision-1") == 262_144
        assert input_token_limit("gemini-3.8-flash") == 1_048_576
        assert input_token_limit("gemini-3.7-flash") == 1_048_576

    def test_a_prefixed_model_id_resolves(self):
        assert input_token_limit("publishers/google/models/GEMINI-4-HYDROGEN") == 1_048_576
        assert input_token_limit("publishers/google/models/GEMINI-4-NEON") == 1_048_576
        assert input_token_limit("publishers/google/models/GEMINI-3.8-FLASH") == 1_048_576
        assert input_token_limit("publishers/google/models/GEMINI-3.7-FLASH") == 1_048_576

    def test_an_unknown_model_still_gets_a_usable_limit(self):
        assert input_token_limit("some-future-model") > 0

    def test_the_budget_leaves_headroom_below_the_window(self):
        """The count is taken on the prompt; the request also carries system instruction,
        tools and the response schema, none of which are in it."""
        assert prompt_token_budget("gemini-4-hydrogen") == int(1_048_576 * 0.90)
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

    def test_max_diff_full_file_bytes_defaults_zero_and_fallback(self, monkeypatch, capsys):
        """max_diff_full_file_bytes supports 0 (disabled), env/config overrides, and negative fallback."""
        from gemini_review.budget import DEFAULT_MAX_DIFF_FULL_FILE_BYTES, max_diff_full_file_bytes

        monkeypatch.delenv("GEMINI_MAX_DIFF_FULL_FILE_BYTES", raising=False)
        assert DEFAULT_MAX_DIFF_FULL_FILE_BYTES == 256 * 1024
        assert max_diff_full_file_bytes() == 262144
        assert max_diff_full_file_bytes({"max_diff_full_file_bytes": 65536}) == 65536

        monkeypatch.setenv("GEMINI_MAX_DIFF_FULL_FILE_BYTES", "0")
        assert max_diff_full_file_bytes({"max_diff_full_file_bytes": 65536}) == 0

        monkeypatch.setenv("GEMINI_MAX_DIFF_FULL_FILE_BYTES", "-10")
        assert max_diff_full_file_bytes() == DEFAULT_MAX_DIFF_FULL_FILE_BYTES
        assert "GEMINI_MAX_DIFF_FULL_FILE_BYTES" in capsys.readouterr().err

        monkeypatch.setenv("GEMINI_MAX_DIFF_FULL_FILE_BYTES", "abc")
        assert max_diff_full_file_bytes() == DEFAULT_MAX_DIFF_FULL_FILE_BYTES


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

    def test_added_and_removed_files_omit_redundant_full_content(self, tmp_path, monkeypatch):
        """Added files already appear in full inside the patch; omit duplicate Full Current File Content blocks."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "new_skill.md").write_text("line one\nline two\nUNIQUE_ADDED_BODY\n")
        files = [
            {
                "filename": "new_skill.md",
                "status": "added",
                "patch": "@@ -0,0 +1,3 @@\n+line one\n+line two\n+UNIQUE_ADDED_BODY",
            },
            {
                "filename": "old_skill.md",
                "status": "removed",
                "patch": "@@ -1,2 +0,0 @@\n-removed one\n-removed two",
            },
        ]
        prompt = build_pr_diff_prompt(files)
        assert "UNIQUE_ADDED_BODY" in prompt
        assert "--- Diff (Patch) ---" in prompt
        assert "--- Full Current File Content ---" not in prompt

    def test_include_full_content_false_drops_full_file_content_even_for_modified(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("UNTOUCHED_FULL_CONTEXT\nchanged_line\n")
        files = [{"filename": "app.py", "status": "modified", "patch": "@@ -2 +2 @@\n-old_line\n+changed_line"}]
        prompt = build_pr_diff_prompt(files, include_full_content=False)
        assert "+ | changed_line" in prompt
        assert "--- Full Current File Content ---" not in prompt
        assert "UNTOUCHED_FULL_CONTEXT" not in prompt

    def test_max_patch_bytes_caps_large_patches_with_marker_and_preserves_small(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "small.py").write_text("tiny = 1\n")
        (tmp_path / "giant.md").write_text("big\n")
        giant_patch = "@@ -0,0 +1,200 @@\n" + "\n".join(f"+row_{i}_payload_padding" for i in range(200))
        files = [
            {"filename": "small.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+tiny_keep"},
            {"filename": "giant.md", "status": "added", "patch": giant_patch},
        ]
        prompt = build_pr_diff_prompt(files, include_full_content=False, max_patch_bytes=800)
        assert "+ | tiny_keep" in prompt
        assert "TRUNCATED by gemini-review: diff patch for 'giant.md'" in prompt
        assert "diff patch for 'small.py'" not in prompt

    def test_excluded_files_header_notice_and_truncation(self, tmp_path, monkeypatch):
        """build_pr_diff_prompt logs a concise omission notice when PR files are excluded by diff_exclude_patterns."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n")
        files = [{"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x = 1"}]

        small_prompt = build_pr_diff_prompt(files, excluded_files=["starter-examples/skills/a/SKILL.md", "gen/b.py"])
        assert "2 changed file(s) were intentionally excluded from this diff by diff_exclude_patterns" in small_prompt
        assert "starter-examples/skills/a/SKILL.md, gen/b.py" in small_prompt
        assert "Do not assume they are missing from the repository." in small_prompt

        many_excluded = [f"starter-examples/skills/s{i}/SKILL.md" for i in range(14)]
        large_prompt = build_pr_diff_prompt(files, excluded_files=many_excluded)
        assert "14 changed file(s) were intentionally excluded" in large_prompt
        assert "s9/SKILL.md, and 4 more" in large_prompt
        assert "s10/SKILL.md" not in large_prompt

    def test_baseline_included_files_skips_duplicate_full_content_while_keeping_excluded(self, tmp_path, monkeypatch):
        """Files already in baseline_included_files omit Full Current File Content (including renames)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "docs").mkdir()
        (tmp_path / "src" / "app.py").write_text("APP_FULL_BODY\n", encoding="utf-8")
        (tmp_path / "src" / "renamed_new.py").write_text("RENAMED_FULL_BODY\n", encoding="utf-8")
        (tmp_path / "docs" / "excluded.md").write_text("EXCLUDED_FROM_BASELINE_BODY\n", encoding="utf-8")

        files = [
            {"filename": "src/app.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new_app"},
            {
                "filename": "src/renamed_new.py",
                "previous_filename": "src/renamed_old.py",
                "status": "renamed",
                "patch": "@@ -1 +1 @@\n-old\n+new_ren",
            },
            {"filename": "docs/excluded.md", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new_doc"},
        ]
        prompt = build_pr_diff_prompt(
            files,
            baseline_included_files={"src/app.py", "src/renamed_old.py"},
        )
        assert "APP_FULL_BODY" not in prompt
        assert "RENAMED_FULL_BODY" not in prompt
        assert "EXCLUDED_FROM_BASELINE_BODY" in prompt
        assert "Pre-PR baseline content is already included in Repository Context above." in prompt
        assert "+ | new_app" in prompt
        assert "+ | new_ren" in prompt

    def test_max_diff_full_file_bytes_zero_disables_all_full_file_blocks(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("FULL_SHOULD_NOT_APPEAR\n", encoding="utf-8")
        files = [{"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+patch_stays"}]

        prompt = build_pr_diff_prompt(files, {"max_diff_full_file_bytes": 0})
        assert "+ | patch_stays" in prompt
        assert "FULL_SHOULD_NOT_APPEAR" not in prompt
        assert "--- Full Current File Content ---" not in prompt
        assert "disabled" in capsys.readouterr().err.lower()

    def test_max_diff_full_file_bytes_applies_waterfill_across_multiple_files(self, tmp_path, monkeypatch, capsys):
        """Aggregate budget preserves small modified files intact and waterfill-caps oversized ones."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "small_a.py").write_text(("a\n" * 500), encoding="utf-8")
        (tmp_path / "small_b.py").write_text(("b\n" * 500), encoding="utf-8")
        (tmp_path / "huge.py").write_text(("c" * 99 + "\n") * 200, encoding="utf-8")  # ~20 KB

        files = [
            {"filename": "small_a.py", "status": "modified", "patch": "@@ -1 +1 @@\n+pa"},
            {"filename": "small_b.py", "status": "modified", "patch": "@@ -1 +1 @@\n+pb"},
            {"filename": "huge.py", "status": "modified", "patch": "@@ -1 +1 @@\n+ph"},
        ]
        prompt = build_pr_diff_prompt(files, {"max_diff_full_file_bytes": 5000})
        assert "TRUNCATED by gemini-review: 'huge.py'" in prompt
        assert "TRUNCATED by gemini-review: 'small_a.py'" not in prompt
        assert "TRUNCATED by gemini-review: 'small_b.py'" not in prompt
        assert "+ | ph" in prompt
        assert "Diff full-file budget:" in capsys.readouterr().err

    def test_single_pass_truncation_reports_true_original_size_under_both_limits(self, tmp_path, monkeypatch, capsys):
        """When a file exceeds both max_file_bytes and max_diff_full_file_bytes, report true original size once."""
        monkeypatch.chdir(tmp_path)
        content = ("x" * 99 + "\n") * 100  # 10,000 bytes
        (tmp_path / "large.py").write_text(content, encoding="utf-8")
        files = [{"filename": "large.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x"}]

        prompt = build_pr_diff_prompt(files, {"max_file_bytes": 4000, "max_diff_full_file_bytes": 1500})
        err = capsys.readouterr().err
        assert "10,000 bytes, showing the first 1,500." in prompt
        assert "4,000 bytes" not in prompt
        assert "Context budget: truncated 1 file(s)" not in err
        assert "Diff full-file budget:" in err


class TestCapPatchAndWaterfill:
    def test_cap_patch_content_truncates_on_line_boundary_with_loud_marker(self):
        from gemini_review.budget import cap_patch_content

        patch = "\n".join(f"{i} + | content_{i}" for i in range(200))
        out, capped = cap_patch_content(patch, "big.diff", 400)
        assert capped is True
        assert "TRUNCATED by gemini-review: diff patch for 'big.diff'" in out
        assert "400" in out
        body = out.split("[TRUNCATED")[0]
        assert body.endswith("\n\n") or body.rstrip("\n").split("\n")[-1].startswith(tuple(str(i) for i in range(200)))

        assert cap_patch_content(patch, "big.diff", 0) == (patch, False)
        assert cap_patch_content("short", "small.diff", 400) == ("short", False)

    def test_compute_waterfill_cap_preserves_small_and_splits_remaining_across_giants(self):
        from gemini_review.budget import compute_waterfill_cap

        assert compute_waterfill_cap([], 10_000) == 0
        # Fits under target_total -> returns max size untouched
        assert compute_waterfill_cap([500, 1200], 5_000) == 1200
        # Two small (1000 each = 2000) + two giant (50000 each) under 22000 budget -> remaining 20000 // 2 = 10000
        assert compute_waterfill_cap([1_000, 50_000, 1_000, 50_000], 22_000) == 10_000
        # Enforces min_cap floor even when budget is tiny
        assert compute_waterfill_cap([10_000, 10_000], 100) == 512
