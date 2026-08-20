"""Tests for resolving review threads the reviewer declared addressed.

The risk being defended against is not "does it resolve", it is "does it resolve the WRONG
thread". Hiding feedback that is still outstanding is worse than leaving every thread open, so
most of these tests are about refusing to act.
"""

from unittest.mock import MagicMock, patch

from gemini_review.schemas import ResolvedItem, ReviewResult
from gemini_review.threads import (
    _thread_key,
    fetch_review_threads,
    resolve_addressed_threads,
    reviewer_logins,
)

BOT = {"github-actions[bot]"}


def thread(path="a.py", line=10, author="github-actions[bot]", resolved=False, can_resolve=True, tid="T1"):
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": False,
        "viewerCanResolve": can_resolve,
        "comments": {"nodes": [{"path": path, "line": line, "originalLine": line, "author": {"login": author}}]},
    }


def gql(threads, resolve_ok=True):
    """A requests.post double returning threads, then a mutation result."""

    def _post(url, headers=None, json=None, timeout=None):
        res = MagicMock()
        res.status_code = 200
        if "reviewThreads" in json["query"]:
            res.json.return_value = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": threads}
                        }
                    }
                }
            }
        else:
            res.json.return_value = {
                "data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": resolve_ok}}}
            }
        return res

    return _post


class TestRefusesToActWhenUnsure:
    def test_an_item_without_a_location_resolves_nothing(self):
        """Prose alone must never be matched against a thread."""
        with patch("gemini_review.threads.requests.post") as post:
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="fixed the thing")], BOT)
        assert out == []
        post.assert_not_called()

    def test_a_human_thread_is_never_resolved(self):
        """The model does not get to close a person's review comment."""
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread(author="a-human")])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == []

    def test_a_mismatched_line_resolves_nothing(self):
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread(line=10)])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=99)], BOT)
        assert out == []

    def test_a_mismatched_path_resolves_nothing(self):
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread(path="a.py")])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="b.py", line=10)], BOT)
        assert out == []

    def test_an_already_resolved_thread_is_skipped(self):
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread(resolved=True)])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == []

    def test_a_thread_the_token_cannot_resolve_is_skipped(self):
        """Not hypothetical: the default GITHUB_TOKEN reports viewerCanResolve=false on every
        thread and its mutation is refused with FORBIDDEN, verified on a live PR. This guard is
        what turns that into a clear message instead of an error."""
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread(can_resolve=False)])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == []

    def test_only_the_matching_thread_of_several_is_resolved(self):
        threads = [thread(path="a.py", line=10, tid="T1"), thread(path="b.py", line=20, tid="T2")]
        with patch("gemini_review.threads.requests.post", side_effect=gql(threads)):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == ["a.py:10"]


class TestHappyPath:
    def test_a_matching_bot_thread_is_resolved(self):
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread()])):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == ["a.py:10"]

    def test_dry_run_reports_without_mutating(self):
        calls = []

        def _post(url, headers=None, json=None, timeout=None):
            calls.append(json["query"])
            return gql([thread()])(url, headers=headers, json=json, timeout=timeout)

        with patch("gemini_review.threads.requests.post", side_effect=_post):
            out = resolve_addressed_threads(
                "o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT, dry_run=True
            )
        assert out == ["a.py:10"]
        assert not any("resolveReviewThread" in q for q in calls), "dry run must not mutate"


class TestFailuresAreNonFatal:
    """The review is already posted by this point. Nothing here may raise."""

    def test_a_graphql_error_payload_returns_empty(self):
        res = MagicMock(status_code=200)
        res.json.return_value = {"errors": [{"message": "nope"}]}
        with patch("gemini_review.threads.requests.post", return_value=res):
            assert (
                resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=1)], BOT) == []
            )

    def test_a_non_200_returns_empty(self):
        res = MagicMock(status_code=403, text="forbidden")
        with patch("gemini_review.threads.requests.post", return_value=res):
            assert fetch_review_threads("o/r", 1, {}) == []

    def test_a_network_exception_returns_empty(self):
        import requests as rq

        with patch("gemini_review.threads.requests.post", side_effect=rq.RequestException("boom")):
            assert fetch_review_threads("o/r", 1, {}) == []

    def test_a_malformed_repository_returns_empty(self):
        assert fetch_review_threads("not-a-repo", 1, {}) == []

    def test_a_failed_mutation_is_not_reported_as_resolved(self):
        with patch("gemini_review.threads.requests.post", side_effect=gql([thread()], resolve_ok=False)):
            out = resolve_addressed_threads("o/r", 1, {}, [ResolvedItem(description="x", path="a.py", line=10)], BOT)
        assert out == []


class TestThreadKey:
    def test_original_line_wins_over_line(self):
        """`line` moves as the file changes and goes null once outdated; originalLine does not."""
        t = thread()
        t["comments"]["nodes"][0]["line"] = 999
        t["comments"]["nodes"][0]["originalLine"] = 10
        assert _thread_key(t) == ("a.py", 10)

    def test_falls_back_to_line_when_original_is_absent(self):
        t = thread()
        t["comments"]["nodes"][0]["originalLine"] = None
        t["comments"]["nodes"][0]["line"] = 42
        assert _thread_key(t) == ("a.py", 42)

    def test_a_thread_with_no_comments_has_no_key(self):
        assert _thread_key({"comments": {"nodes": []}}) is None


class TestReviewerLogins:
    def test_defaults_to_the_actions_bot(self):
        assert "github-actions[bot]" in reviewer_logins()

    def test_env_can_declare_an_app_or_pat_identity(self, monkeypatch):
        monkeypatch.setenv("GEMINI_REVIEWER_LOGIN", "my-app[bot], other")
        logins = reviewer_logins()
        assert "my-app[bot]" in logins and "other" in logins


class TestSchemaCompatibility:
    def test_plain_strings_are_still_accepted(self):
        """The old shape must not raise; it just cannot be resolved automatically."""
        r = ReviewResult(summary="s", resolved_items=["fixed a thing"], general_feedback=[], comments=[])
        assert r.resolved_items[0].description == "fixed a thing"
        assert r.resolved_items[0].path is None

    def test_structured_items_round_trip(self):
        r = ReviewResult(
            summary="s",
            resolved_items=[{"description": "d", "path": "a.py", "line": 3}],
            general_feedback=[],
            comments=[],
        )
        assert (r.resolved_items[0].path, r.resolved_items[0].line) == ("a.py", 3)
