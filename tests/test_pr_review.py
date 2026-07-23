"""
Unit tests for Pull Request code review script (gemini_pr_review.py).
Tests include verifying the file filtering rules (text file checking) and
ensuring the prompt construction substitutes placeholders (e.g. repository,
PR number, and language) correctly.
"""

import os

from gemini_pr_review import (
    InlineComment,
    ReviewResult,
    build_prompt,
    count_text_tokens,
    filter_review_comments,
    format_pr_comment_history,
    generate_file_tree,
    get_all_repo_files,
    get_google_auth_headers,
    get_pr_comments,
    get_pr_files,
    get_valid_changed_lines,
    is_core_file,
    is_text_file,
    list_available_skills,
    load_config,
    load_skill_instructions,
    load_system_instruction,
    post_review,
    search_google_developer_knowledge,
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

    # Assert atomic review creation was attempted
    mock_post.assert_called_once_with(
        "https://api.github.com/repos/derailed-dash/gemini-review-action/pulls/42/reviews",
        headers={"Authorization": "token test"},
        json={
            "body": "## 📋 Review Summary\n\nLooks good\n\n## 🔍 General Feedback\n\n- Clean code",
            "event": "COMMENT",
            "comments": [],
        },
        timeout=60,
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
                "code_suggestion": "print('fixed')",
            }
        ],
    )

    post_review("derailed-dash/gemini-review-action", 42, "head_sha_123", review, {"Authorization": "token test"})

    # We expect 3 requests total:
    # 1. Atomic review post (which fails)
    # 2. Individual comment post for the single comment
    # 3. Final review submit post (without comments)
    assert mock_post.call_count == 3


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
    from gemini_pr_review import parse_skill_metadata

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
    existing_cache.name = "cachedContents/existing-cache-456"
    existing_cache.display_name = "repo-cache-test-owner-test-repo"
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
            "GEMINI_MODEL": "gemini-3.6-flash",
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
    assert create_call_args.kwargs["model"] == "gemini-3.6-flash"
    assert "repo-cache-test-owner-test-repo-gemini-3.6-flash" in create_call_args.kwargs["config"].display_name

    # Verify generate_content received the newly created cache handle
    gen_call_args = mock_client.models.generate_content.call_args
    assert gen_call_args.kwargs["config"].cached_content == "cachedContents/new-model-cache-101"


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
    existing_cache.name = "cachedContents/invalid-cache-999"
    existing_cache.display_name = "repo-cache-test-owner-test-repo"
    existing_cache.model = "gemini-3.6-flash"
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
    assert _normalize_model_name("  MODELS/GEMINI-3.6-FLASH  ") == "gemini-3.6-flash"


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
    count = count_text_tokens(mock_client, "gemini-3.6-flash", "Hello world! " * 50)
    assert count == 150

    # Fallback heuristic when client is None
    fallback_count = count_text_tokens(None, "gemini-3.6-flash", "Hello world!")
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
