"""
Unit tests for Pull Request code review script (gemini_pr_review.py).
Tests include verifying the file filtering rules (text file checking) and
ensuring the prompt construction substitutes placeholders (e.g. repository,
PR number, and language) correctly.
"""
import os

from gemini_pr_review import (
    ReviewResult,
    build_prompt,
    generate_file_tree,
    get_all_repo_files,
    get_pr_files,
    is_core_file,
    is_text_file,
    load_config,
    load_system_instruction,
    post_review,
)


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

    # Mock environment variable for language
    mocker.patch.dict(os.environ, {"GEMINI_LANGUAGE": "English (US)"})

    result = load_system_instruction("derailed-dash/gemini-review-action", 42, mock_toml_content)
    assert result == "Review repo derailed-dash/gemini-review-action PR #42 in English (US)."


def test_is_core_file():
    patterns = ["*.md", "pyproject.toml", "Cargo.toml", "src/*.py"]
    assert is_core_file("README.md", patterns) is True
    assert is_core_file("docs/architecture.md", patterns) is True
    assert is_core_file("pyproject.toml", patterns) is True
    assert is_core_file("main.py", patterns) is False
    assert is_core_file("src/utils.py", patterns) is True


def test_generate_file_tree():
    files = ["src/utils.py", "src/main.py", "tests/test_utils.py", "README.md"]
    expected = (
        ".\n"
        "├── README.md\n"
        "├── src/\n"
        "│   ├── main.py\n"
        "│   └── utils.py\n"
        "└── tests/\n"
        "    └── test_utils.py"
    )
    assert generate_file_tree(files) == expected


def test_get_all_repo_files_git_success(mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_res = mocker.Mock()
    mock_res.stdout = "README.md\nsrc/main.py\nnon_existent.py\n"
    mock_run.return_value = mock_res

    # Mock os.path.exists to return True for README.md and src/main.py but False for non_existent.py
    mock_exists = mocker.patch("os.path.exists")
    mock_exists.side_effect = lambda path: path in ["README.md", "src/main.py"]

    files = get_all_repo_files()
    assert files == ["README.md", "src/main.py"]


def test_get_all_repo_files_git_fallback(mocker):
    # Mock subprocess.run to raise exception to trigger fallback
    mocker.patch("subprocess.run", side_effect=Exception("git not installed"))

    # Mock os.walk
    mocker.patch("os.walk", return_value=[
        (".", ["dir1", ".git"], ["README.md", "image.png"]),
        ("./dir1", [], ["main.py", "non_text.zip"])
    ])

    mocker.patch("os.path.exists", return_value=True)

    files = get_all_repo_files()
    # image.png and non_text.zip should be filtered out by is_text_file
    assert sorted(files) == sorted(["README.md", "dir1/main.py"])


def test_build_prompt_full_context(mocker):
    # Setup mock files in repo
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["README.md", "src/utils.py"])
    mocker.patch("os.path.getsize", return_value=100)
    mocker.patch("gemini_pr_review.get_file_content", side_effect=lambda path: f"Content of {path}")

    pr_files = [{"filename": "src/main.py", "status": "modified", "patch": "+++ diff"}]
    config = {
        "max_context_bytes": 500
    }

    prompt = build_prompt(pr_files, config)
    assert "=== Repository Context (Full Codebase) ===" in prompt
    assert "Content of README.md" in prompt
    assert "Content of src/utils.py" in prompt


def test_build_prompt_sparse_context(mocker):
    # Setup mock files in repo where size exceeds limit
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["README.md", "large_file.py", "src/utils.py"])
    mocker.patch("os.path.getsize", return_value=1000) # 3 files * 1000 = 3000 bytes
    mocker.patch("gemini_pr_review.get_file_content", side_effect=lambda path: f"Content of {path}")

    pr_files = [{"filename": "src/main.py", "status": "modified", "patch": "+++ diff"}]
    config = {
        "max_context_bytes": 500, # threshold is 500, total is 3000 -> Sparse Context Mode
        "core_file_patterns": ["README.md"]
    }

    prompt = build_prompt(pr_files, config)
    assert "=== Repository Context (Large Codebase) ===" in prompt
    assert "--- Repository File Structure ---" in prompt
    assert "├── README.md" in prompt
    assert "--- File: README.md ---" in prompt
    assert "Content of README.md" in prompt
    assert "Content of large_file.py" not in prompt  # Not a core file content block


def test_load_config_fallback(mocker):
    mock_exists = mocker.patch("os.path.exists")
    # Simulate first path (.github/commands/gemini-review.toml) doesn't exist,
    # but second path (starter-examples/gemini-review.toml) exists.
    mock_exists.side_effect = lambda path: "starter-examples" in path

    mock_toml_content = {"max_context_bytes": 1234}
    mocker.patch("tomllib.load", return_value=mock_toml_content)
    mocker.patch("builtins.open", mocker.mock_open())

    config = load_config()
    assert config == {"max_context_bytes": 1234}


def test_load_config_invalid(mocker):
    mocker.patch("os.path.exists", return_value=True)
    # Simulate corrupted TOML
    mocker.patch("tomllib.load", side_effect=ValueError("Invalid TOML syntax"))
    mocker.patch("builtins.open", mocker.mock_open())

    config = load_config()
    assert config == {}


def test_generate_file_tree_windows_paths():
    # Mix of Windows path separators and Unix path separators
    files = ["src\\utils.py", "src/main.py", "README.md"]
    expected = (
        ".\n"
        "├── README.md\n"
        "└── src/\n"
        "    ├── main.py\n"
        "    └── utils.py"
    )
    assert generate_file_tree(files) == expected


def test_get_pr_files(mocker):
    mock_get = mocker.patch("requests.get")

    # Simulate paginated files list from GitHub API
    mock_res_1 = mocker.Mock()
    mock_res_1.status_code = 200
    mock_res_1.json.return_value = [{"filename": "main.py"}, {"filename": "utils.py"}]
    # GitHub pagination link header for page 1
    mock_res_1.headers = {"Link": '<https://api.github.com/...page=2>; rel="next"'}

    mock_res_2 = mocker.Mock()
    mock_res_2.status_code = 200
    mock_res_2.json.return_value = [{"filename": "README.md"}]
    mock_res_2.headers = {}

    # Empty list response to terminate the page iteration loop
    mock_res_3 = mocker.Mock()
    mock_res_3.status_code = 200
    mock_res_3.json.return_value = []
    mock_res_3.headers = {}

    mock_get.side_effect = [mock_res_1, mock_res_2, mock_res_3]

    files = get_pr_files("derailed-dash/gemini-review-action", 42, {"Authorization": "token test"})
    assert files == [{"filename": "main.py"}, {"filename": "utils.py"}, {"filename": "README.md"}]


def test_post_review_atomic(mocker):
    mock_post = mocker.patch("requests.post")
    mock_res = mocker.Mock()
    mock_res.status_code = 200
    mock_post.return_value = mock_res

    review = ReviewResult(
        summary="Looks good",
        general_feedback=["Clean code"],
        comments=[]
    )

    post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    # Assert atomic review creation was attempted
    mock_post.assert_called_once_with(
        "https://api.github.com/repos/derailed-dash/gemini-review-action/pulls/42/reviews",
        headers={"Authorization": "token test"},
        json={
            "body": "## 📋 Review Summary\n\nLooks good\n\n## 🔍 General Feedback\n\n- Clean code",
            "event": "COMMENT",
            "comments": []
        },
        timeout=60
    )


def test_post_review_fallback(mocker):
    mock_post = mocker.patch("requests.post")

    # First post (atomic review) fails with 422
    mock_res_atomic = mocker.Mock()
    mock_res_atomic.status_code = 422

    # Subsequent individual comments post and review post succeed
    mock_res_ok = mocker.Mock()
    mock_res_ok.status_code = 201

    mock_post.side_effect = [mock_res_atomic, mock_res_ok, mock_res_ok]

    review = ReviewResult(
        summary="Some bugs",
        general_feedback=["Needs fix"],
        comments=[
            {
                "path": "main.py",
                "line": 10,
                "side": "RIGHT",
                "severity": "🔴",
                "comment_text": "Fix this crash",
                "code_suggestion": "print('fixed')"
            }
        ]
    )

    post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    # We expect 3 requests total:
    # 1. Atomic review post (which fails)
    # 2. Individual comment post for the single comment
    # 3. Final review submit post (without comments)
    assert mock_post.call_count == 3
