"""
Unit tests for the issue triage script (gemini_issue_triage.py).
Tests include verifying prompt construction (both fallback and custom TOML templates),
mocking GitHub label retrieval with pagination, and validating issue labeling API requests.
"""
from gemini_issue_triage import apply_labels, get_available_labels, load_triage_prompt


def test_load_triage_prompt_fallback(mocker):
    # Mocking os.path.exists to return False for the TOML config path
    mocker.patch("os.path.exists", return_value=False)

    title = "Bug: Crash on load"
    body = "The app crashes on startup."
    labels = ["bug", "critical"]

    result = load_triage_prompt(title, body, labels)
    assert "You are a helpful issue triage assistant" in result
    assert "Title: Bug: Crash on load" in result
    assert "Body: The app crashes on startup." in result
    assert "bug, critical" in result


def test_load_triage_prompt_custom(mocker):
    # Mocking existence of the default action TOML configuration
    mock_exists = mocker.patch("os.path.exists")
    mock_exists.side_effect = lambda path: "gemini-triage.toml" in path

    mock_toml_content = {
        "prompt": "Triage: !{echo $ISSUE_TITLE} / !{echo $ISSUE_BODY} / [!{echo $AVAILABLE_LABELS}]."
    }

    # Mock tomllib.load and built-in open
    mocker.patch("tomllib.load", return_value=mock_toml_content)
    mocker.patch("builtins.open", mocker.mock_open())

    result = load_triage_prompt("Crash on start", "Detailed error message", ["bug", "doc"])
    assert result == "Triage: Crash on start / Detailed error message / [bug, doc]."


def test_get_available_labels(mocker):
    mock_get = mocker.patch("requests.get")

    # Simulating paginated API responses
    mock_response_1 = mocker.Mock()
    mock_response_1.status_code = 200
    mock_response_1.json.return_value = [{"name": "bug"}, {"name": "enhancement"}]

    mock_response_2 = mocker.Mock()
    mock_response_2.status_code = 200
    mock_response_2.json.return_value = []

    mock_get.side_effect = [mock_response_1, mock_response_2]

    labels = get_available_labels("derailed-dash/gemini-review-action", {"Authorization": "token test"})
    assert labels == ["bug", "enhancement"]


def test_apply_labels(mocker):
    mock_post = mocker.patch("requests.post")
    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    # Verify requests.post is called with the labels
    apply_labels("derailed-dash/gemini-review-action", 123, ["bug"], {"Authorization": "token test"})
    mock_post.assert_called_once_with(
        "https://api.github.com/repos/derailed-dash/gemini-review-action/issues/123/labels",
        headers={"Authorization": "token test"},
        json={"labels": ["bug"]}
    )
