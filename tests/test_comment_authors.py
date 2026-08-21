"""Tests for keeping selected authors out of the prompt.

The motivating case: another automated reviewer had already posted its full findings on the PR,
and comment history ingested all of them verbatim. The review that followed reported three of
that reviewer's four findings, on the same lines, and nothing it had not already found — which is
indistinguishable from restating them. Comment history exists so a reviewer does not re-raise its
OWN addressed feedback; a competing reviewer's comments are a different thing entirely.
"""

from gemini_review.github import (
    filter_comment_authors,
    format_pr_comment_history,
    parse_excluded_authors,
)


def c(login, body="x", **kw):
    d = {"user": {"login": login}, "body": body, "path": "a.py", "line": 1, "id": abs(hash((login, body))) % 10**6}
    d.update(kw)
    return d


class TestParse:
    def test_empty_is_no_exclusion(self):
        assert parse_excluded_authors("") == set()
        assert parse_excluded_authors(None) == set()

    def test_comma_separated_and_trimmed(self):
        assert parse_excluded_authors(" a[bot] , b ") == {"a[bot]", "b"}

    def test_case_insensitive(self):
        assert parse_excluded_authors("Claude[Bot]") == {"claude[bot]"}


class TestFilter:
    def test_excluded_author_is_dropped(self):
        out = filter_comment_authors([c("claude[bot]"), c("alice")], {"claude[bot]"})
        assert [x["user"]["login"] for x in out] == ["alice"]

    def test_matching_ignores_case(self):
        out = filter_comment_authors([c("Claude[Bot]")], {"claude[bot]"})
        assert out == []

    def test_no_exclusions_returns_input_untouched(self):
        items = [c("a"), c("b")]
        assert filter_comment_authors(items, set()) is items

    def test_a_malformed_entry_does_not_raise(self):
        assert filter_comment_authors([None, "junk", c("alice")], {"bob"})[0]["user"]["login"] == "alice"

    def test_a_comment_with_no_author_is_kept(self):
        """The filter excludes NAMED authors. An unattributable comment is not one of them, and
        discarding it would silently lose legitimate content to defend against a malformed payload."""
        assert filter_comment_authors([{"body": "orphan"}], {"bob"}) == [{"body": "orphan"}]


class TestFormatting:
    def test_excluded_content_never_reaches_the_prompt(self):
        out = format_pr_comment_history([c("claude[bot]", "USE FieldValue.delete()")], [], {"claude[bot]"})
        assert "FieldValue.delete" not in out

    def test_everything_excluded_yields_an_empty_history(self):
        """Empty rather than a bare header — an empty section still costs tokens and says nothing."""
        assert format_pr_comment_history([c("claude[bot]")], [c("claude[bot]")], {"claude[bot]"}) == ""

    def test_other_authors_survive(self):
        out = format_pr_comment_history([c("alice", "please rename this")], [], {"claude[bot]"})
        assert "please rename this" in out

    def test_default_behaviour_is_unchanged(self):
        """No exclusions configured must behave exactly as before."""
        before = format_pr_comment_history([c("claude[bot]", "keep me")], [])
        after = format_pr_comment_history([c("claude[bot]", "keep me")], [], set())
        assert before == after
        assert "keep me" in before
