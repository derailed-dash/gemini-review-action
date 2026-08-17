"""Tests for retrying transient GitHub failures when posting a review."""

from unittest.mock import Mock, patch

import pytest

from gemini_review.github import POST_RETRIES, RETRYABLE_STATUS, post_with_retry


def response(status):
    r = Mock()
    r.status_code = status
    r.text = f"status {status}"
    return r


@pytest.fixture(autouse=True)
def no_sleeping():
    with patch("gemini_review.github.time.sleep") as slept:
        yield slept


class TestPostWithRetry:
    def test_success_first_time_makes_one_call(self):
        with patch("gemini_review.github.requests.post", return_value=response(201)) as post:
            res = post_with_retry("u", {}, {}, 60)
        assert res.status_code == 201
        assert post.call_count == 1

    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    def test_transient_failure_then_success(self, status):
        with patch("gemini_review.github.requests.post", side_effect=[response(status), response(201)]) as post:
            res = post_with_retry("u", {}, {}, 60)
        assert res.status_code == 201
        assert post.call_count == 2

    def test_gives_up_after_the_retry_budget_and_returns_the_last_response(self):
        with patch("gemini_review.github.requests.post", return_value=response(503)) as post:
            res = post_with_retry("u", {}, {}, 60)
        assert res.status_code == 503
        assert post.call_count == POST_RETRIES + 1

    @pytest.mark.parametrize("status", [403, 404, 422])
    def test_permanent_failures_are_not_retried(self, status):
        """A 403 will fail identically however many times it is sent."""
        with patch("gemini_review.github.requests.post", return_value=response(status)) as post:
            res = post_with_retry("u", {}, {}, 60)
        assert res.status_code == status
        assert post.call_count == 1

    def test_it_waits_between_attempts(self, no_sleeping):
        with patch("gemini_review.github.requests.post", return_value=response(503)):
            post_with_retry("u", {}, {}, 60)
        assert no_sleeping.call_count == POST_RETRIES
        assert all(call.args[0] > 0 for call in no_sleeping.call_args_list)
