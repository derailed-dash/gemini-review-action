"""
Unit tests for Pull Request code review script (gemini_pr_review.py).
Tests include verifying the file filtering rules (text file checking) and
ensuring the prompt construction substitutes placeholders (e.g. repository,
PR number, and language) correctly.
"""

import os

from gemini_pr_review import (
    DynamicContextSelection,
    InlineComment,
    ReviewResult,
    build_codebase_context,
    build_prompt,
    count_text_tokens,
    extract_response_text_or_raise,
    filter_review_comments,
    format_diff_patch_with_line_numbers,
    format_file_content_with_line_numbers,
    format_pr_comment_history,
    generate_file_tree,
    get_all_repo_files,
    get_google_auth_headers,
    get_pr_comments,
    get_pr_files,
    get_valid_changed_lines,
    get_valid_diff_lines,
    is_core_file,
    is_inline_suggestion_commit,
    is_text_file,
    list_available_skills,
    load_config,
    load_skill_instructions,
    load_system_instruction,
    parse_skill_metadata,
    post_commit_status,
    post_review,
    sanitize_code_suggestion,
    search_google_developer_knowledge,
    select_dynamic_context_files,
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

    # Mock environment variable for language and persona
    mocker.patch.dict(os.environ, {"GEMINI_LANGUAGE": "English (US)", "GEMINI_PERSONA": "straight"})

    result = load_system_instruction("derailed-dash/gemini-review-action", 42, mock_toml_content)
    assert "Review repo derailed-dash/gemini-review-action PR #42 in English (US)." in result
    assert "IMPORTANT FOR INLINE CODE SUGGESTIONS" in result


def test_is_core_file():
    patterns = [
        "README*",
        "DESIGN*",
        "SPEC*",
        "DEPLOYMENT*",
        "INSTALL*",
        "PRODUCT*",
        "PROD*",
        "SDD*",
        "TDD*",
        "TODO*",
        "docs/*.md",
        "*template*",
        "*shared*",
        "*util*",
        "*utils*",
        "*common*",
        "*core*",
        "*helper*",
        "*helpers*",
        "*base*",
        "pyproject.toml",
        "Cargo.toml",
        "src/*.py",
    ]
    assert is_core_file("README.md", patterns) is True
    assert is_core_file("readme.rst", patterns) is True
    assert is_core_file("DESIGN.md", patterns) is True
    assert is_core_file("design_doc.txt", patterns) is True
    assert is_core_file("spec.md", patterns) is True
    assert is_core_file("deployment.md", patterns) is True
    assert is_core_file("install.md", patterns) is True
    assert is_core_file("product_reqs.md", patterns) is True
    assert is_core_file("sdd.md", patterns) is True
    assert is_core_file("tdd_plan.md", patterns) is True
    assert is_core_file("TODO.md", patterns) is True
    assert is_core_file("docs/architecture.md", patterns) is True
    assert is_core_file("docs/nested/deep/random_notes.md", patterns) is False
    assert is_core_file("src/template.py", patterns) is True
    assert is_core_file("src/shared/types.ts", patterns) is True
    assert is_core_file("src/utils.py", patterns) is True
    assert is_core_file("src/aoc_commons.py", patterns) is True
    assert is_core_file("lib/core_engine.go", patterns) is True
    assert is_core_file("helpers/test_helper.rb", patterns) is True
    assert is_core_file("base_model.py", patterns) is True
    assert is_core_file("pyproject.toml", patterns) is True

    assert is_core_file("main.py", patterns) is False


def test_generate_file_tree():
    files = ["src/utils.py", "src/main.py", "tests/test_utils.py", "README.md"]
    expected = ".\n├── README.md\n├── src/\n│   ├── main.py\n│   └── utils.py\n└── tests/\n    └── test_utils.py"
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
    mocker.patch(
        "os.walk",
        return_value=[(".", ["dir1", ".git"], ["README.md", "image.png"]), ("./dir1", [], ["main.py", "non_text.zip"])],
    )

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
    config = {"max_context_bytes": 500}

    prompt = build_prompt(pr_files, config)
    assert "=== Repository Context (Full Codebase) ===" in prompt
    assert "Content of README.md" in prompt
    assert "Content of src/utils.py" in prompt


def test_build_prompt_sparse_context(mocker):
    # Setup mock files in repo where size exceeds limit
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["README.md", "large_file.py", "src/utils.py"])
    mocker.patch("os.path.getsize", return_value=1000)  # 3 files * 1000 = 3000 bytes
    mocker.patch("gemini_pr_review.get_file_content", side_effect=lambda path: f"Content of {path}")

    pr_files = [{"filename": "src/main.py", "status": "modified", "patch": "+++ diff"}]
    config = {
        "max_context_bytes": 500,  # threshold is 500, total is 3000 -> Sparse Context Mode
        "core_file_patterns": ["README.md"],
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
    expected = ".\n├── README.md\n└── src/\n    ├── main.py\n    └── utils.py"
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

    review = ReviewResult(summary="Looks good", general_feedback=["Clean code"], comments=[])

    post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    # On pull_request event, only the atomic review is posted (no commit status duplicate)
    assert mock_post.call_count == 1
    assert (
        mock_post.call_args_list[0][0][0]
        == "https://api.github.com/repos/derailed-dash/gemini-review-action/pulls/42/reviews"
    )


def test_post_review_issue_comment_atomic(mocker):
    mock_post = mocker.patch("requests.post")
    mock_res = mocker.Mock()
    mock_res.status_code = 200
    mock_post.return_value = mock_res

    review = ReviewResult(summary="Looks good", general_feedback=["Clean code"], comments=[])

    post_review(
        "derailed-dash/gemini-review-action",
        42,
        "head_sha_123",
        review,
        {"Authorization": "token test"},
        event_name="issue_comment",
    )

    # On issue_comment event, review is posted AND commit status is posted to update check marks
    assert mock_post.call_count == 2
    assert (
        mock_post.call_args_list[0][0][0]
        == "https://api.github.com/repos/derailed-dash/gemini-review-action/pulls/42/reviews"
    )
    assert (
        mock_post.call_args_list[1][0][0]
        == "https://api.github.com/repos/derailed-dash/gemini-review-action/statuses/head_sha_123"
    )


def test_post_review_fallback(mocker):
    mock_post = mocker.patch("requests.post")

    # First post (atomic review) fails with 422
    mock_res_atomic = mocker.Mock()
    mock_res_atomic.status_code = 422

    # Subsequent individual comments post succeed
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
                "code_suggestion": "print('fixed')",
            }
        ],
    )

    post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    # We expect 3 requests total on pull_request event:
    # 1. Atomic review post (which fails)
    # 2. PR issue summary comment
    # 3. Individual inline comment post
    assert mock_post.call_count == 3


def test_post_review_issue_comment_fallback(mocker):
    mock_post = mocker.patch("requests.post")

    # First post (atomic review) fails with 422
    mock_res_atomic = mocker.Mock()
    mock_res_atomic.status_code = 422

    # Subsequent individual comments post and commit status post succeed
    mock_res_ok = mocker.Mock()
    mock_res_ok.status_code = 201

    mock_post.side_effect = [mock_res_atomic, mock_res_ok, mock_res_ok, mock_res_ok]

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
                "code_suggestion": "print('fixed')",
            }
        ],
    )

    post_review(
        "derailed-dash/gemini-review-action",
        42,
        "head_sha_123",
        review,
        {"Authorization": "token test"},
        event_name="issue_comment",
    )

    # We expect 4 requests total on issue_comment:
    # 1. Atomic review post (which fails)
    # 2. PR issue summary comment
    # 3. Individual inline comment post
    # 4. Commit status update
    assert mock_post.call_count == 4
    assert (
        mock_post.call_args_list[3][0][0]
        == "https://api.github.com/repos/derailed-dash/gemini-review-action/statuses/head_sha_123"
    )


def test_post_review_complete_failure(mocker):
    import pytest

    mock_post = mocker.patch("requests.post")

    # Atomic post fails
    mock_res_atomic = mocker.Mock()
    mock_res_atomic.status_code = 403
    mock_res_atomic.text = "Forbidden"

    # Fallback summary post fails
    mock_res_summary = mocker.Mock()
    mock_res_summary.status_code = 403
    mock_res_summary.text = "Forbidden"

    mock_post.side_effect = [mock_res_atomic, mock_res_summary]

    review = ReviewResult(summary="Clean", general_feedback=[], comments=[])

    with pytest.raises(RuntimeError, match="Failed to post PR review to GitHub"):
        post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    assert mock_post.call_count == 2


def test_post_review_issue_comment_complete_failure(mocker):
    import pytest

    mock_post = mocker.patch("requests.post")

    # Atomic post fails
    mock_res_atomic = mocker.Mock()
    mock_res_atomic.status_code = 403
    mock_res_atomic.text = "Forbidden"

    # Fallback summary post fails
    mock_res_summary = mocker.Mock()
    mock_res_summary.status_code = 403
    mock_res_summary.text = "Forbidden"

    # Commit status update succeeds
    mock_res_status = mocker.Mock()
    mock_res_status.status_code = 201

    mock_post.side_effect = [mock_res_atomic, mock_res_summary, mock_res_status]

    review = ReviewResult(summary="Clean", general_feedback=[], comments=[])

    with pytest.raises(RuntimeError, match="Failed to post PR review to GitHub"):
        post_review(
            "derailed-dash/gemini-review-action",
            42,
            "head_sha_123",
            review,
            {"Authorization": "token test"},
            event_name="issue_comment",
        )

    # Check that commit status was set to 'failure'
    status_call = mock_post.call_args_list[-1]
    assert status_call[0][0] == "https://api.github.com/repos/derailed-dash/gemini-review-action/statuses/head_sha_123"
    assert status_call[1]["json"]["state"] == "failure"


def test_get_valid_changed_lines():
    patch = "@@ -10,3 +10,4 @@ context\n line1\n-line2\n+added1\n+added2\n line3\n"
    # The start is 10 on RIGHT.
    # context line1: 10
    # added1: 11
    # added2: 12
    # context line3: 13
    expected = {10, 11, 12, 13}
    assert get_valid_changed_lines(patch) == expected


def test_filter_review_comments():
    text_files = [
        {"filename": "src/main.py", "patch": ("@@ -10,3 +10,4 @@ context\n line1\n-line2\n+added1\n+added2\n line3\n")},
        {"filename": "README.md", "patch": ("@@ -1,3 +1,3 @@\n # Test\n-old\n+new\n")},
    ]

    comments = [
        InlineComment(path="src/main.py", line=11, side="RIGHT", severity="🟢", comment_text="Valid comment"),
        InlineComment(
            path="src/main.py",
            line=5,
            side="RIGHT",
            severity="🟡",
            comment_text="Invalid line comment",
            code_suggestion="print('suggested')",
        ),
        InlineComment(path="invalid_file.py", line=1, side="RIGHT", severity="🔴", comment_text="Invalid path comment"),
    ]

    review = ReviewResult(summary="A summary", general_feedback=["Feedback 1"], comments=comments)

    filtered_review = filter_review_comments(review, text_files)

    # 1. Check that only the valid comment remains inline
    assert len(filtered_review.comments) == 1
    assert filtered_review.comments[0].comment_text == "Valid comment"

    # 2. Check that general feedback was updated with redirected comments
    assert len(filtered_review.general_feedback) == 4
    assert "💡 **Additional Feedback on Unmodified Lines:**" in filtered_review.general_feedback[1]
    assert any("src/main.py" in item for item in filtered_review.general_feedback[2:])
    assert any("Line 5" in item for item in filtered_review.general_feedback[2:])
    assert any("print('suggested')" in item for item in filtered_review.general_feedback[2:])
    assert any("invalid_file.py" in item for item in filtered_review.general_feedback[2:])


def test_filter_review_comments_auto_correct_suggestion_range(mocker):
    mock_file_content = "Line 1\n### Heading Line 95\n- Item Line 96\nLine 97\n"
    mocker.patch("gemini_review.utils.get_file_content", return_value=mock_file_content)

    text_files = [
        {
            "filename": "SKILL.md",
            "patch": "@@ -1,4 +1,4 @@\n Line 1\n+### Heading Line 95\n+- Item Line 96\n Line 97\n",
        }
    ]

    comment = InlineComment(
        path="SKILL.md",
        line=2,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Insert blank line after heading",
        code_suggestion="### Heading Line 95\n\n- Item Line 96",
    )

    review = ReviewResult(summary="Summary", general_feedback=[], comments=[comment])
    filtered = filter_review_comments(review, text_files)

    assert len(filtered.comments) == 1
    assert filtered.comments[0].start_line == 2
    assert filtered.comments[0].line == 3


def test_get_google_auth_headers_api_key(mocker):
    mocker.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    headers = get_google_auth_headers()
    assert headers["X-Goog-Api-Key"] == "test-key"
    assert headers["Content-Type"] == "application/json"


def test_get_google_auth_headers_adc(mocker):
    mocker.patch.dict(os.environ, {}, clear=True)

    mock_creds = mocker.Mock()
    mock_creds.token = "fake-oauth-token"
    mocker.patch("google.auth.default", return_value=(mock_creds, "fake-project"))
    mocker.patch("google.auth.transport.requests.Request")

    headers = get_google_auth_headers()
    assert headers["Authorization"] == "Bearer fake-oauth-token"
    assert headers["Content-Type"] == "application/json"


def test_get_google_auth_headers_none(mocker):
    mocker.patch.dict(os.environ, {}, clear=True)
    mocker.patch("google.auth.default", side_effect=Exception("No credentials"))

    headers = get_google_auth_headers()
    assert headers == {}


def test_search_google_developer_knowledge_success(mocker):
    mocker.patch("gemini_pr_review.get_google_auth_headers", return_value={"X-Goog-Api-Key": "key"})

    mock_post = mocker.patch("requests.post")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "result": {"content": [{"type": "text", "text": "Match 1"}, {"type": "text", "text": "Match 2"}]}
    }
    mock_post.return_value = mock_resp

    res = search_google_developer_knowledge("query")
    assert res == "Match 1\n\nMatch 2"


def test_search_google_developer_knowledge_no_auth(mocker):
    mocker.patch("gemini_pr_review.get_google_auth_headers", return_value={})
    res = search_google_developer_knowledge("query")
    assert "Error: No API key or Application Default Credentials found" in res


def test_search_google_developer_knowledge_api_error(mocker):
    mocker.patch("gemini_pr_review.get_google_auth_headers", return_value={"X-Goog-Api-Key": "key"})

    mock_post = mocker.patch("requests.post")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal error"
    mock_post.return_value = mock_resp

    res = search_google_developer_knowledge("query")
    assert "Error from Google Developer Knowledge API: 500" in res


def test_list_available_skills_builtin(mocker):
    # Mock folder checks and contents
    mocker.patch("os.path.isdir", side_effect=lambda path: "starter-examples" in path or "my-skill" in path)
    mocker.patch("os.listdir", return_value=["my-skill"])
    mocker.patch("os.path.isfile", side_effect=lambda path: "SKILL.md" in path)
    mocker.patch(
        "gemini_pr_review.parse_skill_metadata", return_value={"name": "My Skill", "description": "Dummy skill"}
    )

    skills = list_available_skills()
    assert len(skills) == 1
    assert skills[0]["id"] == "builtin:my-skill/SKILL.md"
    assert skills[0]["name"] == "My Skill"


def test_list_available_skills_workspace(mocker):
    # Mock workspace folder checks and contents
    mocker.patch("os.path.isdir", side_effect=lambda path: ".agents/skills" in path or "custom-skill" in path)
    mocker.patch("os.listdir", return_value=["custom-skill"])
    mocker.patch("os.path.isfile", side_effect=lambda path: "SKILL.md" in path)
    mocker.patch(
        "gemini_pr_review.parse_skill_metadata", return_value={"name": "Custom Skill", "description": "Project rules"}
    )

    skills = list_available_skills()
    assert len(skills) == 1
    assert skills[0]["id"] == "custom-skill/SKILL.md"
    assert skills[0]["name"] == "Custom Skill"


def test_load_skill_instructions_builtin(mocker):
    # Verify resolving a builtin skill works safely
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("builtins.open", mocker.mock_open(read_data="Built-in instructions"))

    content = load_skill_instructions("builtin:agent-aware-cli/SKILL.md")
    assert content == "Built-in instructions"


def test_load_skill_instructions_workspace(mocker):
    # Verify resolving workspace skill
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("builtins.open", mocker.mock_open(read_data="Workspace instructions"))

    content = load_skill_instructions("custom-rules.md")
    assert content == "Workspace instructions"


def test_load_skill_instructions_path_traversal():
    content = load_skill_instructions("builtin:../../../../etc/passwd")
    assert "Error: Access denied (path traversal blocked)." in content


def test_parse_skill_metadata(mocker):

    # 1. Folder-structured skill (e.g. SKILL.md)
    mocker.patch("builtins.open", mocker.mock_open(read_data="---\nname: Specific Folder Skill\n---\n"))
    meta = parse_skill_metadata("some/folder/path/SKILL.md")
    assert meta["name"] == "Specific Folder Skill"

    # 2. File-structured skill without YAML metadata (falls back to file stem)
    mocker.patch("builtins.open", mocker.mock_open(read_data="# Heading Skill\nSome content"))
    meta = parse_skill_metadata("some/folder/path/file-based-skill.md")
    assert meta["name"] == "Heading Skill"

    # 3. Multiline frontmatter block scalar (using >-)
    yaml_multiline_block = """---
name: Multiline Block Skill
description: >-
  This is a long description
  that spans multiple lines
---
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=yaml_multiline_block))
    meta = parse_skill_metadata("some/folder/path/SKILL.md")
    assert meta["name"] == "Multiline Block Skill"
    assert meta["description"] == "This is a long description that spans multiple lines"

    # 4. Standard multiline indented append (without block scalar)
    yaml_multiline_append = """---
name: Standard Multiline Skill
description: First line
  and second line
---
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=yaml_multiline_append))
    meta = parse_skill_metadata("some/folder/path/SKILL.md")
    assert meta["name"] == "Standard Multiline Skill"
    assert meta["description"] == "First line and second line"


def test_get_google_developer_documents_success(mocker):
    from gemini_pr_review import get_google_developer_documents

    mocker.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    mock_post = mocker.patch("requests.post")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "result": {"content": [{"type": "text", "text": "Detailed setup instructions for GKE."}]}
    }
    mock_post.return_value = mock_resp

    res = get_google_developer_documents(["documents/docs.cloud.google.com/gke"])
    assert "Detailed setup instructions for GKE." in res


def test_get_google_developer_documents_no_auth(mocker):
    from gemini_pr_review import get_google_developer_documents

    mocker.patch.dict(os.environ, {}, clear=True)
    mocker.patch("google.auth.default", side_effect=Exception("No ADC"))

    res = get_google_developer_documents(["documents/docs.cloud.google.com/gke"])
    assert "Error: No API key or Application Default Credentials found." in res


def test_get_google_developer_documents_api_error(mocker):
    from gemini_pr_review import get_google_developer_documents

    mocker.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"})
    mock_post = mocker.patch("requests.post")
    mock_resp = mocker.Mock()
    mock_resp.status_code = 404
    mock_resp.text = "Not found"
    mock_post.return_value = mock_resp

    res = get_google_developer_documents(["documents/docs.cloud.google.com/gke"])
    assert "Error from Google Developer Knowledge API: 404" in res


def test_load_workspace_rules_success(mocker):
    from gemini_pr_review import load_workspace_rules

    mocker.patch("os.path.exists", side_effect=lambda path: path == "AGENTS.md")
    mocker.patch("os.path.isfile", side_effect=lambda path: path == "AGENTS.md")
    mocker.patch("builtins.open", mocker.mock_open(read_data="My Project Rules"))

    rules = load_workspace_rules()
    assert "=== Rules from AGENTS.md ===" in rules
    assert "My Project Rules" in rules


def test_context_caching_logic(mocker):
    """Test that context caching creates a cache when prompt is large enough."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
        },
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "large.py", "status": "modified", "patch": "diff patch"}],
    )
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["other.py"])
    mocker.patch("gemini_pr_review.get_file_content", return_value="a" * 120000)

    mock_client = mocker.Mock()
    mock_cache = mocker.Mock()
    mock_cache.name = "cachedContents/test-cache-123"
    mock_client.caches.create.return_value = mock_cache

    mock_parsed_cfg = mocker.Mock()
    mock_parsed_cfg.tools = []
    mock_client.models._parse_config.return_value = mock_parsed_cfg

    mock_response = mocker.Mock()

    mock_response.text = '{"summary": "OK", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = mocker.Mock(
        prompt_token_count=150000,
        candidates_token_count=100,
        total_token_count=150100,
        cached_content_token_count=145000,
    )
    mock_client.models.generate_content.return_value = mock_response

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    # Run main in dry-run mode
    main()

    # Verify cache creation was invoked with display_name and ttl
    assert mock_client.caches.create.called
    call_args = mock_client.caches.create.call_args
    assert "repo-cache-test-owner-test-repo" in call_args.kwargs["config"].display_name

    # Verify generate_content received cached_content
    gen_call_args = mock_client.models.generate_content.call_args
    assert gen_call_args.kwargs["config"].cached_content == "cachedContents/test-cache-123"


def test_context_caching_reuse_existing_cache(mocker):
    """Test that existing active cache is reused without calling caches.create."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
            "GEMINI_PERSONA": "straight",
        },
        clear=True,
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "large.py", "status": "modified", "patch": "diff patch"}],
    )
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["other.py"])
    mocker.patch("gemini_pr_review.get_file_content", return_value="a" * 120000)

    mock_client = mocker.Mock()
    existing_cache = mocker.Mock()
    existing_cache.name = "cachedContents/existing-cache-456"
    existing_cache.display_name = "repo-cache-test-owner-test-repo-gemini-3.7-flash-straight"
    existing_cache.model = "gemini-3.7-flash"
    mock_client.caches.list.return_value = [existing_cache]

    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "OK", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = mocker.Mock(
        prompt_token_count=150000,
        candidates_token_count=100,
        total_token_count=150100,
        cached_content_token_count=145000,
    )
    mock_client.models.generate_content.return_value = mock_response

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    main()

    # Verify caches.create was NOT called since existing cache was found
    assert not mock_client.caches.create.called
    gen_call_args = mock_client.models.generate_content.call_args
    assert gen_call_args.kwargs["config"].cached_content == "cachedContents/existing-cache-456"


def test_context_caching_model_mismatch_skips_cache(mocker):
    """Test that an active cache for a different model is skipped and a new cache is created."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GEMINI_MODEL": "gemini-3.7-flash",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
        },
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "large.py", "status": "modified", "patch": "diff patch"}],
    )
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["other.py"])
    mocker.patch("gemini_pr_review.get_file_content", return_value="a" * 120000)

    mock_client = mocker.Mock()
    existing_cache = mocker.Mock()
    existing_cache.name = "cachedContents/old-model-cache-789"
    existing_cache.display_name = "repo-cache-test-owner-test-repo"
    existing_cache.model = "models/gemini-3.5-flash"  # Model mismatch!
    mock_client.caches.list.return_value = [existing_cache]

    mock_new_cache = mocker.Mock()
    mock_new_cache.name = "cachedContents/new-model-cache-101"
    mock_client.caches.create.return_value = mock_new_cache

    mock_parsed_cfg = mocker.Mock()
    mock_parsed_cfg.tools = []
    mock_client.models._parse_config.return_value = mock_parsed_cfg

    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "OK", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = mocker.Mock(
        prompt_token_count=150000,
        candidates_token_count=100,
        total_token_count=150100,
        cached_content_token_count=145000,
    )
    mock_client.models.generate_content.return_value = mock_response

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    main()

    # Verify caches.create WAS called because old cache model did not match
    assert mock_client.caches.create.called
    create_call_args = mock_client.caches.create.call_args
    assert create_call_args.kwargs["model"] == "gemini-3.7-flash"
    assert "repo-cache-test-owner-test-repo-gemini-3.7-flash" in create_call_args.kwargs["config"].display_name

    # Verify generate_content received the newly created cache handle
    gen_call_args = mock_client.models.generate_content.call_args
    assert gen_call_args.kwargs["config"].cached_content == "cachedContents/new-model-cache-101"


def test_context_caching_persona_mismatch_skips_cache(mocker):
    """Test that an active cache for a different persona is skipped and a fresh persona cache created."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GEMINI_MODEL": "gemini-3.7-flash",
            "GEMINI_PERSONA": "rick",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
        },
        clear=True,
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "large.py", "status": "modified", "patch": "diff patch"}],
    )
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["other.py"])
    mocker.patch("gemini_pr_review.get_file_content", return_value="a" * 120000)

    mock_client = mocker.Mock()
    mock_client.models._parse_config.return_value.tools = None
    existing_cache = mocker.Mock()
    existing_cache.name = "cachedContents/old-dazbo-cache-123"
    existing_cache.display_name = "repo-cache-test-owner-test-repo-gemini-3.7-flash-dazbo"
    existing_cache.model = "gemini-3.7-flash"
    mock_client.caches.list.return_value = [existing_cache]

    mock_new_cache = mocker.Mock()
    mock_new_cache.name = "cachedContents/new-rick-cache-789"
    mock_client.caches.create.return_value = mock_new_cache

    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "Wubba Lubba Dub-Dub!", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = mocker.Mock(
        prompt_token_count=150000,
        candidates_token_count=100,
        total_token_count=150100,
        cached_content_token_count=0,
    )
    mock_client.models.generate_content.return_value = mock_response

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    main()

    # Verify caches.create WAS called because old cache persona (dazbo) did not match new persona (rick)
    assert mock_client.caches.create.called
    create_call_args = mock_client.caches.create.call_args
    assert create_call_args.kwargs["config"].display_name == "repo-cache-test-owner-test-repo-gemini-3.7-flash-rick"

    # Verify generate_content received the newly created rick cache handle
    gen_call_args = mock_client.models.generate_content.call_args
    assert gen_call_args.kwargs["config"].cached_content == "cachedContents/new-rick-cache-789"


def test_context_caching_generate_content_fallback(mocker):
    """Test that if generate_content with cached_content fails, it falls back to direct context."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
            "GEMINI_PERSONA": "straight",
        },
        clear=True,
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "large.py", "status": "modified", "patch": "diff patch"}],
    )
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["other.py"])
    mocker.patch("gemini_pr_review.get_file_content", return_value="a" * 120000)

    mock_client = mocker.Mock()
    existing_cache = mocker.Mock()
    existing_cache.name = "cachedContents/invalid-cache-999"
    existing_cache.display_name = "repo-cache-test-owner-test-repo-gemini-3.7-flash-straight"
    existing_cache.model = "gemini-3.7-flash"
    mock_client.caches.list.return_value = [existing_cache]

    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "OK", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = mocker.Mock(
        prompt_token_count=150000,
        candidates_token_count=100,
        total_token_count=150100,
        cached_content_token_count=0,
    )

    # First call with cache fails, second call without cache succeeds
    mock_client.models.generate_content.side_effect = [
        RuntimeError("400 INVALID_ARGUMENT: Model mismatch or invalid cache handle"),
        mock_response,
    ]

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    main()

    # generate_content should have been called twice (first with cache, then fallback without cache)
    assert mock_client.models.generate_content.call_count == 2
    first_call_args = mock_client.models.generate_content.call_args_list[0]
    second_call_args = mock_client.models.generate_content.call_args_list[1]

    assert first_call_args.kwargs["config"].cached_content == "cachedContents/invalid-cache-999"
    assert second_call_args.kwargs["config"].cached_content is None


def test_normalize_model_name():
    """Test model name normalisation across various SDK and Vertex AI format strings."""
    from gemini_pr_review import _normalize_model_name

    assert _normalize_model_name(None) == ""
    assert _normalize_model_name("") == ""
    assert _normalize_model_name("gemini-3.5-flash") == "gemini-3.5-flash"
    assert _normalize_model_name("models/gemini-3.5-flash") == "gemini-3.5-flash"
    assert _normalize_model_name("publishers/google/models/gemini-3.5-flash") == "gemini-3.5-flash"
    assert _normalize_model_name("  MODELS/GEMINI-3.7-FLASH  ") == "gemini-3.7-flash"


def test_get_pr_comments(mocker):
    """Test fetching PR review comments and issue comments from GitHub API."""
    mock_get = mocker.patch("requests.get")

    mock_review_response = mocker.Mock()
    mock_review_response.status_code = 200
    mock_review_response.json.return_value = [{"id": 101, "body": "Review comment"}]

    mock_issue_response = mocker.Mock()
    mock_issue_response.status_code = 200
    mock_issue_response.json.return_value = [{"id": 201, "body": "Issue comment"}]

    mock_get.side_effect = [mock_review_response, mock_issue_response]

    review_comments, issue_comments = get_pr_comments("owner/repo", 42, {"Authorization": "token abc"}, timeout=10)

    assert len(review_comments) == 1
    assert review_comments[0]["id"] == 101
    assert len(issue_comments) == 1
    assert issue_comments[0]["id"] == 201
    assert mock_get.call_count == 2


def test_get_pr_comments_pagination(mocker):
    """Test get_pr_comments paginating across multiple pages."""
    mock_get = mocker.patch("requests.get")

    page1_reviews = [{"id": i} for i in range(100)]
    page2_reviews = [{"id": 101}]

    res_review_p1 = mocker.Mock(status_code=200, json=lambda: page1_reviews)
    res_review_p2 = mocker.Mock(status_code=200, json=lambda: page2_reviews)
    res_issue_p1 = mocker.Mock(status_code=200, json=lambda: [{"id": 500}])

    mock_get.side_effect = [res_review_p1, res_review_p2, res_issue_p1]

    reviews, issues = get_pr_comments("owner/repo", 42, {}, timeout=10)

    assert len(reviews) == 101
    assert len(issues) == 1
    assert mock_get.call_count == 3


def test_get_pr_comments_error_handling(mocker):
    """Test get_pr_comments handling API failure gracefully."""
    mock_get = mocker.patch("requests.get")
    mock_get.side_effect = Exception("Network timeout")

    review_comments, issue_comments = get_pr_comments("owner/repo", 42, {}, timeout=10)

    assert review_comments == []
    assert issue_comments == []


def test_format_pr_comment_history():
    """Test thread grouping and string formatting of PR comments."""
    review_comments = [
        {
            "id": 1,
            "path": "src/main.py",
            "line": 42,
            "user": {"login": "gemini-bot"},
            "body": "Consider adding error handling here.",
        },
        {
            "id": 2,
            "in_reply_to_id": 1,
            "path": "src/main.py",
            "line": 42,
            "user": {"login": "dazbo"},
            "body": "Error is handled by caller function.",
        },
    ]

    issue_comments = [
        {
            "id": 10,
            "user": {"login": "dazbo"},
            "body": "PR updated with new tests.",
            "created_at": "2026-07-23T12:00:00Z",
        }
    ]

    formatted = format_pr_comment_history(review_comments, issue_comments)

    assert "=== Prior PR Discussion & Review Threads ===" in formatted
    assert "Thread on `src/main.py` (Line 42)" in formatted
    assert "- [gemini-bot]: Consider adding error handling here." in formatted
    assert "└─ [dazbo]: Error is handled by caller function." in formatted
    assert "--- General PR Conversation Comments ---" in formatted
    assert "• [dazbo] (2026-07-23): PR updated with new tests." in formatted


def test_count_text_tokens(mocker):
    """Test token counting helper function with SDK mock and fallback."""
    mock_client = mocker.Mock()
    mock_client.models.count_tokens.return_value = mocker.Mock(total_tokens=150)

    # With SDK support
    count = count_text_tokens(mock_client, "gemini-3.7-flash", "Hello world! " * 50)
    assert count == 150

    # Fallback heuristic when client is None
    fallback_count = count_text_tokens(None, "gemini-3.7-flash", "Hello world!")
    assert fallback_count == len("Hello world!") // 4


def test_build_prompt_with_comment_history(mocker):
    """Test build_prompt includes comment_history when provided."""
    mocker.patch("gemini_pr_review.is_text_file", return_value=True)
    mocker.patch("gemini_pr_review.get_file_content", return_value="def main(): pass")
    mocker.patch("gemini_pr_review.build_codebase_context", return_value="")

    files = [{"filename": "main.py", "status": "modified", "patch": "@@ -1 +1 @@\n+def main(): pass"}]
    comment_history = "=== Prior PR Discussion & Review Threads ===\n[dazbo]: Handled upstream."

    prompt = build_prompt(files, {}, comment_history=comment_history)

    assert "=== File: main.py ===" in prompt
    assert "=== Prior PR Discussion & Review Threads ===" in prompt
    assert "[dazbo]: Handled upstream." in prompt


def test_post_review_with_resolved_items(mocker):
    """Test post_review formats resolved_items section into review body."""
    mock_post = mocker.patch("requests.post")
    mock_post.return_value = mocker.Mock(status_code=200)

    review = ReviewResult(
        summary="PR LGTM",
        resolved_items=["Added null check in main.py", "Updated docstrings"],
        general_feedback=["Good tests"],
        comments=[],
    )

    post_review("owner/repo", 42, "head_sha", review, {"Authorization": "token abc"})

    assert mock_post.call_count == 1
    posted_payload = mock_post.call_args[1]["json"]
    body = posted_payload["body"]

    assert "## 📋 Review Summary" in body
    assert "### ✅ Resolved Items from Prior Reviews" in body
    assert "- Added null check in main.py" in body
    assert "- Updated docstrings" in body
    assert "## 🔍 General Feedback" in body


def test_post_review_with_usage_metadata(mocker):
    """Test post_review formats collapsible token usage details when usage_metadata is provided."""
    mock_post = mocker.patch("requests.post")
    mock_post.return_value = mocker.Mock(status_code=200)

    review = ReviewResult(
        summary="PR LGTM",
        general_feedback=[],
        comments=[],
    )

    usage_metadata = {
        "prompt_tokens": 1000,
        "cached_tokens": 800,
        "fresh_tokens": 150,
        "comment_history_tokens": 50,
        "candidates_tokens": 100,
        "total_tokens": 1100,
        "cache_percentage": 80.0,
    }

    post_review("owner/repo", 42, "head_sha", review, {"Authorization": "token abc"}, usage_metadata=usage_metadata)

    assert mock_post.call_count == 1
    posted_payload = mock_post.call_args[1]["json"]
    body = posted_payload["body"]

    assert "<details>" in body
    assert "<summary>📊 Token Usage & Cost Efficiency</summary>" in body
    assert "| **Input Tokens (uncached)** | 150 |" in body
    assert "| **Input Tokens (cached)** | 800 (⚡ 80.0% cached) |" in body
    assert "| **PR Comments History Tokens** | 50 |" in body
    assert "| **Output Tokens** | 100 |" in body
    assert "| **Total Session Tokens** | **1,100** |" in body


# --- Personas Feature Tests ---


def test_get_persona_prompt_straight():
    """Test get_persona_prompt returns empty string for straight/default/empty personas."""
    from gemini_review import get_persona_prompt

    assert get_persona_prompt(None) == ""
    assert get_persona_prompt("") == ""
    assert get_persona_prompt("straight") == ""
    assert get_persona_prompt("Straight") == ""
    assert get_persona_prompt("default") == ""
    assert get_persona_prompt("none") == ""


def test_get_persona_prompt_dazbo():
    """Test get_persona_prompt returns Dazbo persona overlay prompt."""
    from gemini_review import get_persona_prompt

    prompt = get_persona_prompt("dazbo")
    assert "## Mandatory Persona Directive: Dazbo" in prompt
    assert "exasperation" in prompt.lower()
    assert "sarcasm" in prompt.lower()

    # Case-insensitivity & whitespace handling
    assert get_persona_prompt("  DAZBO  ") == prompt


def test_get_persona_prompt_palpatine():
    """Test get_persona_prompt returns Palpatine persona overlay prompt."""
    from gemini_review import get_persona_prompt

    prompt = get_persona_prompt("palpatine")
    assert "## Mandatory Persona Directive: Emperor Palpatine" in prompt
    assert "Execute Order 66" in prompt
    assert "Unlimited power!" in prompt
    assert "Dark Side" in prompt.title() or "dark side" in prompt.lower()

    # Case-insensitivity
    assert get_persona_prompt("Palpatine") == prompt


def test_get_persona_prompt_rick():
    """Test get_persona_prompt returns Rick Sanchez persona overlay prompt."""
    from gemini_review import get_persona_prompt

    prompt = get_persona_prompt("rick")
    assert "## Mandatory Persona Directive: Rick Sanchez" in prompt
    assert "Wubba Lubba Dub-Dub!" in prompt
    assert "Jerry-tier" in prompt or "Jerry-level" in prompt

    # Case-insensitivity & alias handling
    assert get_persona_prompt("Rick") == prompt
    assert get_persona_prompt("rick_sanchez") == prompt


def test_get_persona_prompt_unknown(capsys):
    """Test get_persona_prompt prints warning and falls back to straight for unknown personas."""
    from gemini_review import get_persona_prompt

    prompt = get_persona_prompt("invalid_persona")
    assert prompt == ""

    captured = capsys.readouterr()
    assert "Warning: Unknown reviewer persona 'invalid_persona'" in captured.err


def test_resolve_persona_name(mocker):
    """Test resolve_persona_name environment and configuration precedence."""
    from gemini_review import resolve_persona_name

    # Default fallback
    mocker.patch.dict(os.environ, {}, clear=True)
    assert resolve_persona_name({}) == "straight"

    # From config
    assert resolve_persona_name({"persona": "dazbo"}) == "dazbo"

    # Environment variable overrides config
    mocker.patch.dict(os.environ, {"GEMINI_PERSONA": "palpatine"})
    assert resolve_persona_name({"persona": "dazbo"}) == "palpatine"


def test_load_system_instruction_with_persona(mocker):
    """Test load_system_instruction appends persona prompt overlays correctly."""
    from gemini_review import load_system_instruction

    mocker.patch.dict(os.environ, {"GEMINI_PERSONA": "dazbo"})
    config = {"prompt": "You are a review bot."}

    instruction = load_system_instruction("owner/repo", 1, config)
    assert "You are a review bot." in instruction
    assert "## Mandatory Persona Directive: Dazbo" in instruction


# --- Line Number Formatting & Multi-Line Comment Tests ---


def test_format_file_content_with_line_numbers():
    content = "first line\nsecond line\nthird line"
    formatted = format_file_content_with_line_numbers(content)
    lines = formatted.splitlines()
    assert len(lines) == 3
    assert "   1 | first line" in lines[0]
    assert "   2 | second line" in lines[1]
    assert "   3 | third line" in lines[2]


def test_format_diff_patch_with_line_numbers():
    patch = "@@ -10,3 +20,4 @@\n context line\n-old line\n+new line 1\n+new line 2\n"
    formatted = format_diff_patch_with_line_numbers(patch)
    assert "@@ -10,3 +20,4 @@" in formatted
    assert "   20   | context line" in formatted
    assert "   11 - | old line" in formatted
    assert "   21 + | new line 1" in formatted
    assert "   22 + | new line 2" in formatted


def test_get_valid_diff_lines():
    patch = "@@ -10,3 +20,4 @@\n context line\n-old line\n+new line 1\n+new line 2\n"
    right_lines, left_lines = get_valid_diff_lines(patch)
    assert right_lines == {20, 21, 22}
    assert left_lines == {10, 11}


def test_filter_review_comments_multiline_and_left_side():
    text_files = [
        {
            "filename": "src/main.py",
            "patch": "@@ -10,3 +20,4 @@\n context line\n-old line\n+new line 1\n+new line 2\n",
        }
    ]

    comments = [
        InlineComment(
            path="src/main.py",
            start_line=20,
            line=22,
            side="RIGHT",
            severity="🟢",
            comment_text="Valid multi-line comment",
        ),
        InlineComment(
            path="src/main.py",
            line=11,
            side="LEFT",
            severity="🔴",
            comment_text="Valid left side deletion comment",
        ),
        InlineComment(
            path="src/main.py",
            start_line=5,
            line=20,
            side="RIGHT",
            severity="🟠",
            comment_text="start_line outside diff hunk",
        ),
    ]

    review = ReviewResult(summary="Test", general_feedback=[], comments=comments)
    filtered = filter_review_comments(review, text_files)

    assert len(filtered.comments) == 3
    # 1. Multi-line comment retained
    assert filtered.comments[0].start_line == 20
    assert filtered.comments[0].line == 22

    # 2. LEFT side comment retained
    assert filtered.comments[1].side == "LEFT"
    assert filtered.comments[1].line == 11

    # 3. start_line outside diff hunk (5 not in {20, 21, 22}) resets start_line to None
    assert filtered.comments[2].start_line is None
    assert filtered.comments[2].line == 20


def test_post_review_multiline_payload(mocker):
    mock_post = mocker.patch("requests.post")
    mock_res = mocker.Mock()
    mock_res.status_code = 200
    mock_post.return_value = mock_res

    review = ReviewResult(
        summary="Looks good",
        general_feedback=[],
        comments=[
            InlineComment(
                path="main.py",
                start_line=10,
                line=15,
                side="RIGHT",
                severity="🟢",
                comment_text="Multi-line refactor",
                code_suggestion="new_code()",
            )
        ],
    )

    post_review("owner/repo", 1, "sha", review, {"Authorization": "token test"})

    assert mock_post.call_count == 1
    payload = mock_post.call_args[1]["json"]
    assert len(payload["comments"]) == 1
    assert payload["comments"][0]["start_line"] == 10
    assert payload["comments"][0]["start_side"] == "RIGHT"
    assert payload["comments"][0]["line"] == 15


def test_extract_response_text_or_raise_success(mocker):
    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "All good"}'
    assert extract_response_text_or_raise(mock_response) == '{"summary": "All good"}'


def test_extract_response_text_or_raise_none_with_candidates(mocker):
    import pytest

    mock_candidate = mocker.Mock()
    mock_candidate.finish_reason = "SAFETY"
    mock_candidate.finish_message = "Blocked due to safety settings"
    mock_candidate.safety_ratings = [{"category": "HARM", "probability": "HIGH"}]

    mock_response = mocker.Mock()
    mock_response.text = None
    mock_response.candidates = [mock_candidate]
    mock_response.function_calls = None
    mock_response.prompt_feedback = None

    with pytest.raises(RuntimeError) as exc_info:
        extract_response_text_or_raise(mock_response)

    assert "finish_reason=SAFETY" in str(exc_info.value)
    assert "Blocked due to safety settings" in str(exc_info.value)


def test_extract_response_text_or_raise_none_with_function_calls(mocker):
    import pytest

    mock_response = mocker.Mock()
    mock_response.text = None
    mock_response.candidates = []
    mock_response.function_calls = [{"name": "search_google_developer_knowledge"}]
    mock_response.prompt_feedback = None

    with pytest.raises(RuntimeError) as exc_info:
        extract_response_text_or_raise(mock_response)

    assert "Model emitted function call(s)" in str(exc_info.value)


def test_extract_response_text_or_raise_property_getter_exception(mocker):
    import pytest

    mock_response = mocker.Mock()
    type(mock_response).text = property(mocker.Mock(side_effect=ValueError("Response candidate was blocked")))
    mock_response.candidates = []
    mock_response.function_calls = None
    mock_response.prompt_feedback = None

    with pytest.raises(RuntimeError) as exc_info:
        extract_response_text_or_raise(mock_response)

    assert "Gemini model returned empty or non-text response" in str(exc_info.value)


def test_post_commit_status(mocker, monkeypatch):
    mock_post = mocker.patch("requests.post")
    mock_res = mocker.Mock()
    mock_res.status_code = 201
    mock_post.return_value = mock_res

    monkeypatch.delenv("GITHUB_WORKFLOW", raising=False)
    post_commit_status("owner/repo", "sha123456", "success", "All good", {"Authorization": "token test"})

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/owner/repo/statuses/sha123456"
    assert kwargs["json"]["state"] == "success"
    assert kwargs["json"]["description"] == "All good"
    assert kwargs["json"]["context"] == "Gemini Code Review / review"


def test_post_commit_status_custom_workflow_and_context(mocker, monkeypatch):
    mock_post = mocker.patch("requests.post")
    mock_res = mocker.Mock()
    mock_res.status_code = 201
    mock_post.return_value = mock_res

    monkeypatch.setenv("GITHUB_WORKFLOW", "🔎 Dazbo's Gemini Code Review")
    post_commit_status("owner/repo", "sha123456", "success", "All good", {"Authorization": "token test"})

    args, kwargs = mock_post.call_args
    assert kwargs["json"]["context"] == "🔎 Dazbo's Gemini Code Review / review"

    # With explicit context override
    post_commit_status(
        "owner/repo",
        "sha123456",
        "success",
        "All good",
        {"Authorization": "token test"},
        context="Custom Context",
    )
    args, kwargs = mock_post.call_args
    assert kwargs["json"]["context"] == "Custom Context"


def test_is_inline_suggestion_commit_true(mocker):
    mock_get = mocker.patch("requests.get")
    mock_res = mocker.Mock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "committer": {"login": "web-flow"},
        "commit": {
            "committer": {"email": "noreply@github.com"},
            "message": "Update SKILL.md\n\nCo-authored-by: github-actions[bot] <bot@users.noreply.github.com>",
        },
    }
    mock_get.return_value = mock_res

    assert is_inline_suggestion_commit("owner/repo", "sha123", {"Authorization": "token test"}) is True


def test_is_inline_suggestion_commit_false(mocker):
    mock_get = mocker.patch("requests.get")
    mock_res = mocker.Mock()
    mock_res.status_code = 200
    mock_res.json.return_value = {
        "committer": {"login": "derailed-dash"},
        "commit": {
            "committer": {"email": "dazbo@example.com"},
            "message": "Manual developer commit",
        },
    }
    mock_get.return_value = mock_res

    assert is_inline_suggestion_commit("owner/repo", "sha123", {"Authorization": "token test"}) is False


def test_sanitize_code_suggestion():
    # None or empty
    assert sanitize_code_suggestion(None) is None
    assert sanitize_code_suggestion("   ") is None

    # Line number pipe prefixes (the exact bug case)
    dirty = "105 | 3. For any newly relocated skill:\n106 |     - Prompt the user\n107 |     - Insert the skill"
    clean = "3. For any newly relocated skill:\n    - Prompt the user\n    - Insert the skill"
    assert sanitize_code_suggestion(dirty) == clean

    # Diff annotated line numbers
    dirty_diff = "  105 + |     - Prompt the user\n  106 + |     - Insert the skill"
    clean_diff = "    - Prompt the user\n    - Insert the skill"
    assert sanitize_code_suggestion(dirty_diff) == clean_diff

    # Colon line numbers or L-prefixes
    dirty_colon = "L105: return True\nL106: return False"
    clean_colon = "return True\nreturn False"
    assert sanitize_code_suggestion(dirty_colon) == clean_colon

    # Outer markdown code block fences
    fenced = "```suggestion\ndef foo():\n    pass\n```"
    assert sanitize_code_suggestion(fenced) == "def foo():\n    pass"

    # Untouched valid code (including dict keys with integer labels like '105: "foo"')
    valid_code = "def bar():\n    d = {105: 'foo'}\n    return 42"
    assert sanitize_code_suggestion(valid_code) == valid_code

    dict_key_line = "105: 'foo'"
    assert sanitize_code_suggestion(dict_key_line) == dict_key_line


def test_filter_review_comments_sanitizes_line_prefixes(mocker):
    mock_file_content = (
        "Dummy\n" * 104
    ) + "3. For any newly relocated skill:\n    - Prompt the user\n    - Insert the skill\nLine 108\n"
    mocker.patch("gemini_review.utils.get_file_content", return_value=mock_file_content)

    text_files = [
        {
            "filename": "SKILL.md",
            "patch": (
                "@@ -104,5 +104,5 @@\n Line 104\n+3. For any newly relocated skill:\n+    - Prompt the"
                " user\n+    - Insert the skill\n Line 108\n"
            ),
        }
    ]

    comment = InlineComment(
        path="SKILL.md",
        line=105,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Fix indentation",
        code_suggestion=(
            "105 | 3. For any newly relocated skill:\n106 |     - Prompt the user\n107 |     - Insert the skill"
        ),
    )

    review = ReviewResult(summary="Summary", general_feedback=[], comments=[comment])
    filtered = filter_review_comments(review, text_files)

    assert len(filtered.comments) == 1
    c = filtered.comments[0]
    assert c.code_suggestion == "3. For any newly relocated skill:\n    - Prompt the user\n    - Insert the skill"
    assert c.start_line == 105
    assert c.line == 107


def test_filter_review_comments_auto_aligns_indentation(mocker):
    mock_file_content = ("Dummy\n" * 257) + "    prefix_pattern = re.compile(r'old')\nLine 259\n"
    mocker.patch("gemini_review.utils.get_file_content", return_value=mock_file_content)

    text_files = [
        {
            "filename": "utils.py",
            "patch": "@@ -257,3 +257,3 @@\n Dummy\n+    prefix_pattern = re.compile(r'old')\n Line 259\n",
        }
    ]

    comment = InlineComment(
        path="utils.py",
        line=258,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Refine regex pattern",
        code_suggestion="prefix_pattern = re.compile(r'new')",
    )

    review = ReviewResult(summary="Summary", general_feedback=[], comments=[comment])
    filtered = filter_review_comments(review, text_files)

    assert len(filtered.comments) == 1
    assert filtered.comments[0].code_suggestion == "    prefix_pattern = re.compile(r'new')"


def test_auto_align_suggestion_indentation_deep_nesting(mocker):
    # Target line at line 10 has 12 spaces of indentation
    mock_file_content = ("Line\n" * 9) + "            deeply_nested_func(a, b)\nLine 11\n"
    mocker.patch("gemini_review.utils.get_file_content", return_value=mock_file_content)

    text_files = [
        {
            "filename": "deep.py",
            "patch": "@@ -9,3 +9,3 @@\n Line\n+            deeply_nested_func(a, b)\n Line 11\n",
        }
    ]

    # Case 1: 0 spaces base in suggestion -> shifted by 12 spaces
    comment_0 = InlineComment(
        path="deep.py",
        line=10,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Refactor",
        code_suggestion="deeply_nested_func(a, b,\n    c=1)",
    )
    review_0 = filter_review_comments(ReviewResult(summary="S", general_feedback=[], comments=[comment_0]), text_files)
    assert review_0.comments[0].code_suggestion == "            deeply_nested_func(a, b,\n                c=1)"

    s_input = (" " * 4) + "deeply_nested_func(a, b,\n" + (" " * 8) + "c=1)"
    expected = (" " * 12) + "deeply_nested_func(a, b,\n" + (" " * 16) + "c=1)"
    comment_4 = InlineComment(
        path="deep.py",
        line=10,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Refactor",
        code_suggestion=s_input,
    )
    review_4 = filter_review_comments(ReviewResult(summary="S", general_feedback=[], comments=[comment_4]), text_files)
    assert review_4.comments[0].code_suggestion == expected


def test_auto_align_suggestion_indentation_tabs(mocker):
    # Target line at line 5 has 2 tabs of indentation
    mock_file_content = ("Line\n" * 4) + "\t\tfunc()\nLine 6\n"
    mocker.patch("gemini_review.utils.get_file_content", return_value=mock_file_content)

    text_files = [
        {
            "filename": "tabs.go",
            "patch": "@@ -4,3 +4,3 @@\n Line\n+\t\tfunc()\n Line 6\n",
        }
    ]

    comment = InlineComment(
        path="tabs.go",
        line=5,
        start_line=None,
        side="RIGHT",
        severity="🟡",
        comment_text="Refactor Go",
        code_suggestion="func()\n\tmore()",
    )
    review = filter_review_comments(ReviewResult(summary="S", general_feedback=[], comments=[comment]), text_files)
    assert review.comments[0].code_suggestion == "\t\tfunc()\n\t\t\tmore()"


def test_select_dynamic_context_files_success(mocker):
    """Test select_dynamic_context_files successfully queries model and returns validated file paths."""
    mock_client = mocker.Mock()
    mock_response = mocker.Mock()
    mock_response.text = (
        '{"selected_files": ["gemini_review/prompts.py", "gemini_review/utils.py"], "reasoning": "Core helpers"}'
    )
    mock_client.models.generate_content.return_value = mock_response

    files = [{"filename": "gemini_pr_review.py", "status": "modified", "patch": "@@ -1 +1 @@\n+import utils"}]
    candidates = ["gemini_review/prompts.py", "gemini_review/utils.py", "unrelated/asset.txt"]

    selected, reasoning = select_dynamic_context_files(
        client=mock_client,
        model="gemini-3.7-flash",
        files=files,
        candidate_files=candidates,
    )

    assert selected == ["gemini_review/prompts.py", "gemini_review/utils.py"]
    assert reasoning == "Core helpers"
    assert mock_client.models.generate_content.called
    call_args = mock_client.models.generate_content.call_args
    assert call_args.kwargs["model"] == "gemini-3.7-flash"
    assert call_args.kwargs["config"].response_schema == DynamicContextSelection


def test_select_dynamic_context_files_filters_hallucinated_files(mocker):
    """Test select_dynamic_context_files filters out paths not present in candidate_files."""
    mock_client = mocker.Mock()
    mock_response = mocker.Mock()
    mock_response.text = (
        '{"selected_files": ["gemini_review/utils.py", "non_existent/fake.py", "./gemini_review/prompts.py"],'
        ' "reasoning": "Test rationale"}'
    )
    mock_client.models.generate_content.return_value = mock_response

    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]
    candidates = ["gemini_review/prompts.py", "gemini_review/utils.py"]

    selected, reasoning = select_dynamic_context_files(
        client=mock_client,
        model="gemini-3.7-flash",
        files=files,
        candidate_files=candidates,
    )

    assert selected == ["gemini_review/utils.py", "gemini_review/prompts.py"]
    assert "non_existent/fake.py" not in selected
    assert reasoning == "Test rationale"


def test_select_dynamic_context_files_handles_exception(mocker):
    """Test select_dynamic_context_files gracefully handles API exceptions without crashing."""
    mock_client = mocker.Mock()
    mock_client.models.generate_content.side_effect = RuntimeError("API quota exceeded")

    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]
    candidates = ["gemini_review/prompts.py"]

    selected, reasoning = select_dynamic_context_files(
        client=mock_client,
        model="gemini-3.7-flash",
        files=files,
        candidate_files=candidates,
    )

    assert selected == []
    assert reasoning == ""


def test_select_dynamic_context_files_no_client():
    """Test select_dynamic_context_files returns empty selection when client is None."""
    selected, reasoning = select_dynamic_context_files(
        client=None,
        model="gemini-3.7-flash",
        files=[],
        candidate_files=["foo.py"],
    )
    assert selected == []
    assert reasoning == ""


def test_build_codebase_context_sparse_mode_with_dynamic_selection(mocker):
    """Test build_codebase_context in sparse mode invokes dynamic context selection and appends files."""
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["main.py", "core.md", "helper.py", "extra.py"])
    mocker.patch("os.path.getsize", return_value=100000)  # 3 * 100k = 300k bytes > 100k max
    mocker.patch("gemini_pr_review.is_core_file", side_effect=lambda f, pats: f.endswith(".md"))
    mocker.patch(
        "gemini_pr_review.get_file_content",
        side_effect=lambda f: f"# Content of {f}",
    )
    mock_select = mocker.patch(
        "gemini_pr_review.select_dynamic_context_files",
        return_value=(["helper.py"], "Helper is imported by main.py"),
    )

    mock_client = mocker.Mock()
    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]
    config = {"max_context_bytes": 1000}

    context = build_codebase_context(
        files=files,
        config=config,
        client=mock_client,
        model="gemini-3.7-flash",
    )

    assert "=== Repository Context (Large Codebase) ===" in context
    assert "--- Repository File Structure ---" in context
    assert "--- Key Configuration and Documentation Files ---" in context
    assert "# Content of core.md" in context
    assert "--- Relevant Codebase Context (Dynamically Selected) ---" in context
    assert "Selection Rationale: Helper is imported by main.py" in context
    assert "# Content of helper.py" in context

    # Verify select_dynamic_context_files was called with candidates excluding core files
    assert mock_select.called
    call_kwargs = mock_select.call_args.kwargs
    assert call_kwargs["candidate_files"] == ["helper.py", "extra.py"]
    assert call_kwargs["model"] == "gemini-3.7-flash"


def test_main_passes_model_to_build_codebase_context(mocker):
    """Test main() retrieves GEMINI_MODEL and passes client + model to build_codebase_context."""
    import sys

    from gemini_pr_review import main

    mocker.patch.dict(
        os.environ,
        {
            "GEMINI_API_KEY": "test-key",
            "GEMINI_MODEL": "gemini-3.7-flash",
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "GITHUB_EVENT_PATH": "",
        },
    )

    mocker.patch(
        "gemini_pr_review.get_local_git_files",
        return_value=[{"filename": "main.py", "status": "modified", "patch": "diff"}],
    )
    mock_build_ctx = mocker.patch("gemini_pr_review.build_codebase_context", return_value="")

    mock_client = mocker.Mock()
    mock_response = mocker.Mock()
    mock_response.text = '{"summary": "OK", "general_feedback": [], "comments": []}'
    mock_response.usage_metadata = None
    mock_client.models.generate_content.return_value = mock_response

    mocker.patch("google.genai.Client", return_value=mock_client)
    mocker.patch.object(sys, "argv", ["gemini_pr_review.py"])

    main()

    assert mock_build_ctx.called
    call_kwargs = mock_build_ctx.call_args.kwargs
    assert call_kwargs["client"] == mock_client
    assert call_kwargs["model"] == "gemini-3.7-flash"


def test_build_codebase_context_sparse_mode_enforces_max_core_context_bytes(mocker):
    """Test build_codebase_context in sparse mode respects max_core_context_bytes limit."""
    mocker.patch("gemini_pr_review.get_all_repo_files", return_value=["main.py", "README.md", "GEMINI.md", "helper.py"])
    mocker.patch("os.path.getsize", side_effect=lambda f: 300000 if f.endswith(".md") else 100000)
    mocker.patch("gemini_pr_review.is_core_file", side_effect=lambda f, pats: f.endswith(".md"))
    mocker.patch("gemini_pr_review.get_file_content", side_effect=lambda f: f"# Content of {f}")
    mock_select = mocker.patch("gemini_pr_review.select_dynamic_context_files", return_value=([], ""))

    mock_client = mocker.Mock()
    files = [{"filename": "main.py", "status": "modified", "patch": "diff"}]
    # max_context_bytes triggers sparse mode (100k < total size ~700k)
    # max_core_context_bytes is 400k (so only first 300k core file fits, second 300k is skipped)
    config = {"max_context_bytes": 100000, "max_core_context_bytes": 400000}

    context = build_codebase_context(
        files=files,
        config=config,
        client=mock_client,
        model="gemini-3.7-flash",
    )

    assert "# Content of README.md" in context
    assert "# Content of GEMINI.md" not in context  # Skipped due to 400k limit
    # The skipped core file GEMINI.md and helper.py become candidates for dynamic selection
    assert mock_select.called
    assert "GEMINI.md" in mock_select.call_args.kwargs["candidate_files"]
    assert "helper.py" in mock_select.call_args.kwargs["candidate_files"]
