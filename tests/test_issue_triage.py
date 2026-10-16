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

    mock_toml_content = {"prompt": "Triage: !{echo $ISSUE_TITLE} / !{echo $ISSUE_BODY} / [!{echo $AVAILABLE_LABELS}]."}

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
        json={"labels": ["bug"]},
        timeout=60,
    )


def test_get_issue_comments(mocker):
    from gemini_issue_triage import get_issue_comments

    mock_get = mocker.patch("requests.get")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"id": 1, "body": "Screenshot: ![img](https://example.com/a.png)"}]
    mock_get.return_value = mock_resp

    comments = get_issue_comments("derailed-dash/gemini-review-action", 42, {"Authorization": "token test"})
    assert len(comments) == 1
    assert "https://example.com/a.png" in comments[0]["body"]


def test_triage_main_with_remote_screenshots(mocker, monkeypatch, tmp_path):
    import json

    import gemini_issue_triage

    event_file = tmp_path / "event.json"
    event_payload = {
        "issue": {
            "number": 60,
            "title": "UI glitch on settings modal",
            "body": "See screenshot:\n![Bug](https://github.com/user-attachments/assets/1111-2222)",
        }
    }
    event_file.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("GEMINI_API_KEY", "ai_test_key")
    monkeypatch.setenv("GITHUB_REPOSITORY", "derailed-dash/gemini-review-action")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv("GEMINI_EXCLUDE_COMMENT_AUTHORS", "github-actions[bot]")

    mocker.patch("gemini_issue_triage.get_available_labels", return_value=["bug", "ui", "enhancement"])
    mocker.patch(
        "gemini_issue_triage.get_issue_comments",
        return_value=[
            {"user": {"login": "alice"}, "body": '<img src="https://user-images.githubusercontent.com/1/2.png">'},
            {"user": {"login": "github-actions[bot]"}, "body": "Bot comment that should be excluded"},
        ],
    )

    from google.genai import types

    fake_part_1 = types.Part.from_bytes(data=b"fake_png_1", mime_type="image/png")
    fake_part_2 = types.Part.from_bytes(data=b"fake_png_2", mime_type="image/png")
    mock_build_parts = mocker.patch(
        "gemini_issue_triage.build_triage_multimodal_parts",
        return_value=[fake_part_1, fake_part_2],
    )

    mock_client_cls = mocker.patch("gemini_issue_triage.genai.Client")
    mock_client = mock_client_cls.return_value
    mock_response = mocker.Mock()
    mock_response.text = json.dumps(
        {
            "selected_labels": ["bug", "ui"],
            "reasoning": "The screenshots show a broken modal layout.",
        }
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_apply = mocker.patch("gemini_issue_triage.apply_labels")

    gemini_issue_triage.main()

    mock_build_parts.assert_called_once()
    passed_comments = mock_build_parts.call_args[1]["issue_comments"]
    assert len(passed_comments) == 1
    assert passed_comments[0]["user"]["login"] == "alice"
    call_kwargs = mock_client.models.generate_content.call_args[1]
    sent_contents = call_kwargs["contents"]
    assert isinstance(sent_contents, list)
    assert len(sent_contents) == 3
    assert "UI glitch on settings modal" in sent_contents[0]
    assert "Bot comment that should be excluded" not in sent_contents[0]
    assert sent_contents[1] == fake_part_1
    assert sent_contents[2] == fake_part_2
    mock_apply.assert_called_once_with(
        "derailed-dash/gemini-review-action",
        60,
        ["bug", "ui"],
        {"Authorization": "token ghp_test_token", "Accept": "application/vnd.github.v3+json"},
        timeout=60,
    )


def test_triage_main_fallback_to_text_only_on_failure(mocker, monkeypatch, tmp_path):
    import json

    import gemini_issue_triage

    event_file = tmp_path / "event.json"
    event_payload = {
        "issue": {
            "number": 61,
            "title": "Crash when clicking submit",
            "body": "Broken screenshot:\n![Broken](https://github.com/user-attachments/assets/broken-img)",
        }
    }
    event_file.write_text(json.dumps(event_payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
    monkeypatch.setenv("GEMINI_API_KEY", "ai_test_key")
    monkeypatch.setenv("GITHUB_REPOSITORY", "derailed-dash/gemini-review-action")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")

    mocker.patch("gemini_issue_triage.get_available_labels", return_value=["bug", "enhancement"])
    mocker.patch("gemini_issue_triage.get_issue_comments", return_value=[])
    # Simulate all remote screenshot downloads failing -> returns empty list
    mocker.patch("gemini_issue_triage.build_triage_multimodal_parts", return_value=[])

    mock_client_cls = mocker.patch("gemini_issue_triage.genai.Client")
    mock_client = mock_client_cls.return_value
    mock_response = mocker.Mock()
    mock_response.text = json.dumps(
        {
            "selected_labels": ["bug"],
            "reasoning": "Text description indicates a crash on submit.",
        }
    )
    mock_client.models.generate_content.return_value = mock_response
    mock_apply = mocker.patch("gemini_issue_triage.apply_labels")

    gemini_issue_triage.main()

    call_kwargs = mock_client.models.generate_content.call_args[1]
    assert isinstance(call_kwargs["contents"], str)
    assert "Crash when clicking submit" in call_kwargs["contents"]
    mock_apply.assert_called_once()
