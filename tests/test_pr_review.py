"""
Unit tests for Pull Request code review script (gemini_pr_review.py).
Tests include verifying the file filtering rules (text file checking) and
ensuring the prompt construction substitutes placeholders (e.g. repository,
PR number, and language) correctly.
"""
import os

from gemini_pr_review import is_text_file, load_system_instruction


def test_is_text_file():
    # Text files
    assert is_text_file("main.py") is True
    assert is_text_file("src/utils.go") is True
    assert is_text_file("README.md") is True
    assert is_text_file("config.toml") is True

    # Excluded extensions
    assert is_text_file("image.png") is False
    assert is_text_file("doc.pdf") is False
    assert is_text_file("archive.zip") is False
    assert is_text_file("data.db") is False
    assert is_text_file("script.pyc") is False

    # Excluded exact filenames
    assert is_text_file("package-lock.json") is False
    assert is_text_file("uv.lock") is False
    assert is_text_file(".env") is False
    assert is_text_file("src/api/.envrc") is False


def test_load_system_instruction(mocker):
    # Mocking existence of the default action TOML configuration
    mock_exists = mocker.patch("os.path.exists")
    mock_exists.side_effect = lambda path: "gemini-review.toml" in path

    mock_toml_content = {
        "prompt": "Review repo !{echo $REPOSITORY} PR #!{echo $PULL_REQUEST_NUMBER} in !{echo $LANGUAGE}."
    }

    # Mock tomllib.load and built-in open
    mocker.patch("tomllib.load", return_value=mock_toml_content)
    mocker.patch("builtins.open", mocker.mock_open())

    # Mock environment variable for language
    mocker.patch.dict(os.environ, {"GEMINI_LANGUAGE": "English (US)"})

    result = load_system_instruction("derailed-dash/gemini-review-action", 42)
    assert result == "Review repo derailed-dash/gemini-review-action PR #42 in English (US)."
