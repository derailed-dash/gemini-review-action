"""Tests for Vertex billing labels."""

from types import SimpleNamespace

import pytest

from gemini_review.billing_labels import build_labels, parse_pairs, sanitise, sanitise_key


def vertex():
    return SimpleNamespace(vertexai=True)


def api_key():
    return SimpleNamespace(vertexai=False)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("GEMINI_BILLING_LABELS", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")


class TestSanitise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("owner/repo", "owner_repo"),  # the case that matters: "/" is illegal in a label
            ("Owner/Repo", "owner_repo"),
            ("already-fine_1", "already-fine_1"),
            ("spaces and !@#", "spaces_and"),
            ("__leading_and_trailing__", "leading_and_trailing"),
        ],
    )
    def test_values(self, raw, expected):
        assert sanitise(raw) == expected

    def test_length_is_capped_at_the_gcp_limit(self):
        assert len(sanitise("a" * 200)) == 63

    def test_key_must_start_with_a_letter(self):
        assert sanitise_key("123repo").startswith("k_")
        assert sanitise_key("repo") == "repo"


class TestParsePairs:
    def test_parses_and_sanitises(self):
        assert parse_pairs("team=Platform,cost_centre=BSE/Fusion") == {
            "team": "platform",
            "cost_centre": "bse_fusion",
        }

    def test_malformed_entries_are_skipped_not_fatal(self):
        """A typo in an optional label must not fail a review already paid for."""
        assert parse_pairs("good=yes,garbage,=novalue,nokey=") == {"good": "yes"}


class TestBuildLabels:
    def test_vertex_gets_defaults(self):
        assert build_labels(vertex()) == {"component": "gemini-review-action", "repo": "owner_repo"}

    def test_api_key_backend_gets_nothing(self):
        """Not a policy choice: the Developer API has no labels field, and the SDK raises."""
        assert build_labels(api_key()) is None

    def test_api_key_backend_still_returns_none_when_labels_were_requested(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BILLING_LABELS", "team=platform")
        assert build_labels(api_key()) is None

    def test_user_labels_merge_over_defaults(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BILLING_LABELS", "team=platform,repo=override")
        assert build_labels(vertex()) == {
            "component": "gemini-review-action",
            "repo": "override",
            "team": "platform",
        }

    @pytest.mark.parametrize("off", ["none", "off", "false", "0"])
    def test_can_be_disabled(self, monkeypatch, off):
        monkeypatch.setenv("GEMINI_BILLING_LABELS", off)
        assert build_labels(vertex()) is None

    def test_empty_string_env_var_does_not_disable_default_labels(self, monkeypatch):
        """Action inputs default to '' in action.yml; empty string must not turn off defaults."""
        monkeypatch.setenv("GEMINI_BILLING_LABELS", "")
        assert build_labels(vertex()) == {"component": "gemini-review-action", "repo": "owner_repo"}

    def test_config_is_used_when_the_env_var_is_absent(self):
        assert build_labels(vertex(), {"billing_labels": "team=bse"})["team"] == "bse"

    def test_env_wins_over_config(self, monkeypatch):
        monkeypatch.setenv("GEMINI_BILLING_LABELS", "team=from_env")
        assert build_labels(vertex(), {"billing_labels": "team=from_config"})["team"] == "from_env"

    def test_explicit_repository_argument_wins_over_the_environment(self):
        assert build_labels(vertex(), repository="other/thing")["repo"] == "other_thing"

    def test_no_repository_still_yields_the_component_label(self, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        assert build_labels(vertex()) == {"component": "gemini-review-action"}

    def test_pr_number_is_not_a_default_label(self):
        """Per-PR labels would make every pull request its own billing dimension."""
        assert "pr" not in build_labels(vertex())
