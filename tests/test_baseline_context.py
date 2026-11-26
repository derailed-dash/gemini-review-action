"""Tests for invariant base_sha baseline helpers, context_mode routing, and prefix caching stability."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    """Run git in a temporary repository with minimal deterministic identity."""
    res = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _init_temp_repo(tmp_path: Path, files: dict[str, bytes | str]) -> tuple[Path, str]:
    """Create a real git repository at `tmp_path/repo` with an initial commit returning (repo, base_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "core.autocrlf", "false")
    for rel_path, content in files.items():
        target = repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_bytes(content.encode("utf-8"))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial baseline")
    base_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha


class TestGitBaselineTreeHelpers:
    def test_ensure_git_ref_available_rejects_unsafe_refs(self, mocker):
        from gemini_review.repo import ensure_git_ref_available

        mock_run = mocker.patch("gemini_review.repo.subprocess.run")
        for bad_ref in ("", "   ", "-flag", "--upload-pack=evil", "main;rm", "a..b", "ref with space", "bad\x00ref"):
            assert ensure_git_ref_available(bad_ref) is False
        mock_run.assert_not_called()

    def test_ensure_git_ref_available_returns_true_when_present_without_fetching(self, mocker):
        from gemini_review.repo import ensure_git_ref_available

        mock_run = mocker.patch(
            "gemini_review.repo.subprocess.run",
            return_value=mocker.Mock(returncode=0),
        )
        assert ensure_git_ref_available("abc123def456") is True
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0] == ["git", "rev-parse", "--verify", "--quiet", "abc123def456^{tree}"]

    def test_ensure_git_ref_available_fetches_shallow_when_missing(self, mocker):
        from gemini_review.repo import ensure_git_ref_available

        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["git", "rev-parse"]:
                # Missing on first check, present after fetch
                rev_calls = sum(1 for c in calls if c[:2] == ["git", "rev-parse"])
                return mocker.Mock(returncode=1 if rev_calls == 1 else 0)
            if cmd[:2] == ["git", "fetch"]:
                return mocker.Mock(returncode=0)
            raise AssertionError(f"Unexpected command: {cmd}")

        mocker.patch("gemini_review.repo.subprocess.run", side_effect=_fake_run)
        assert ensure_git_ref_available("deadbeef1234", timeout=15) is True
        assert calls == [
            ["git", "rev-parse", "--verify", "--quiet", "deadbeef1234^{tree}"],
            ["git", "fetch", "--no-tags", "--depth=1", "origin", "deadbeef1234"],
            ["git", "rev-parse", "--verify", "--quiet", "deadbeef1234^{tree}"],
        ]

    def test_ensure_git_ref_available_returns_false_on_fetch_failure(self, mocker):
        from gemini_review.repo import ensure_git_ref_available

        mocker.patch(
            "gemini_review.repo.subprocess.run",
            side_effect=[mocker.Mock(returncode=1), mocker.Mock(returncode=128), mocker.Mock(returncode=1)],
        )
        assert ensure_git_ref_available("deadbeef1234") is False

    def test_list_and_read_git_tree_files_real_repo_and_multibyte_utf8(self, tmp_path, monkeypatch):
        from gemini_review.repo import ensure_git_ref_available, list_git_tree_files, read_git_tree_texts

        multibyte_text = "héllo — café 🚀\nline two\r\n"
        repo, base_sha = _init_temp_repo(
            tmp_path,
            {
                "src/app.py": multibyte_text,
                "docs/guide.md": "# Guide\nPlain ASCII\n",
                "assets/pixel.bin": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary",
                "big/generated.txt": "x" * 2000,
            },
        )
        # Modify working tree afterwards to verify helpers read the base_sha commit, not HEAD/disk
        (repo / "src" / "app.py").write_text("MODIFIED_WORKING_TREE", encoding="utf-8")
        _git(repo, "commit", "-am", "pr commit")

        monkeypatch.chdir(repo)
        assert ensure_git_ref_available(base_sha) is True

        entries = list_git_tree_files(base_sha)
        paths_only = [p for p, _ in entries]
        assert paths_only == ["assets/pixel.bin", "big/generated.txt", "docs/guide.md", "src/app.py"]
        size_map = dict(entries)
        assert size_map["src/app.py"] == len(multibyte_text.encode("utf-8"))

        texts = read_git_tree_texts(
            base_sha,
            ["src/app.py", "docs/guide.md", "assets/pixel.bin", "big/generated.txt", "does/not/exist.py"],
            max_blob_bytes=500,
        )
        assert texts["src/app.py"] == multibyte_text
        assert texts["docs/guide.md"] == "# Guide\nPlain ASCII\n"
        assert "assets/pixel.bin" not in texts
        assert "big/generated.txt" not in texts
        assert "does/not/exist.py" not in texts


class TestInvariantBaselineContextAndModes:
    def test_baseline_prefix_is_byte_identical_across_different_prs(self, tmp_path, monkeypatch):
        from gemini_review.context import build_codebase_context

        repo, base_sha = _init_temp_repo(
            tmp_path,
            {
                "src/a.py": "def a():\n    return 1\n",
                "src/b.py": "def b():\n    return 2\n",
                "docs/arch.md": "# Architecture\nDetails\n",
                "generated/junk.py": "IGNORED_BASELINE\n",
            },
        )
        # Simulate working tree at PR head where a.py changed
        (repo / "src" / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
        monkeypatch.chdir(repo)

        pr_a = [{"filename": "src/a.py", "status": "modified", "patch": "@@ -2 +2 @@\n-    return 1\n+    return 99"}]
        pr_b = [{"filename": "src/b.py", "status": "modified", "patch": "@@ -2 +2 @@\n-    return 2\n+    return 42"}]
        cfg = {"context_exclude_patterns": ["generated/**"]}

        telemetry_a: dict = {}
        ctx_a = build_codebase_context(
            files=pr_a,
            config=cfg,
            context_telemetry=telemetry_a,
            excluded_files=["some/diff_only_exclude.py"],
            base_sha=base_sha,
        )

        telemetry_b: dict = {}
        ctx_b = build_codebase_context(
            files=pr_b,
            config=cfg,
            context_telemetry=telemetry_b,
            base_sha=base_sha,
        )

        assert ctx_a == ctx_b
        assert telemetry_a["context_mode"] == "full_baseline"
        assert telemetry_a["baseline_included"] is True
        assert telemetry_a["baseline_included_files"] == {"docs/arch.md", "src/a.py", "src/b.py"}
        assert "def a():\n    return 1" in ctx_a
        assert "return 99" not in ctx_a
        assert "IGNORED_BASELINE" not in ctx_a

    def test_context_mode_diff_only_and_sparse_and_full_routing(self, tmp_path, monkeypatch):
        from gemini_review.context import build_codebase_context

        repo, base_sha = _init_temp_repo(
            tmp_path,
            {
                "README.md": "# Repo\nOverview\n",
                "src/mod.py": "def f():\n    return 'base'\n",
            },
        )
        monkeypatch.chdir(repo)
        pr_files = [{"filename": "src/mod.py", "status": "modified", "patch": "@@ -1 +1 @@\n+x"}]

        # diff_only skips repo dump
        tel_diff: dict = {}
        ctx_diff = build_codebase_context(
            files=pr_files,
            config={"context_mode": "diff_only"},
            context_telemetry=tel_diff,
            base_sha=base_sha,
        )
        assert ctx_diff == ""
        assert tel_diff["context_mode"] == "diff_only"

        # sparse forces Sparse Context Mode even for tiny repos
        tel_sparse: dict = {}
        ctx_sparse = build_codebase_context(
            files=pr_files,
            config={"context_mode": "sparse", "max_context_bytes": 1_000_000},
            context_telemetry=tel_sparse,
            base_sha=base_sha,
        )
        assert tel_sparse["context_mode"] == "sparse"
        assert "Large Codebase" in ctx_sparse
        assert "README.md" in ctx_sparse

        # auto with tiny max_context_bytes falls through baseline to sparse mode
        tel_auto_small: dict = {}
        ctx_auto_small = build_codebase_context(
            files=pr_files,
            config={"context_mode": "auto", "max_context_bytes": 5},
            context_telemetry=tel_auto_small,
            base_sha=base_sha,
        )
        assert tel_auto_small["context_mode"] == "sparse"
        assert "Large Codebase" in ctx_auto_small

        # full overrides small max_context_bytes threshold
        tel_full: dict = {}
        ctx_full = build_codebase_context(
            files=pr_files,
            config={"context_mode": "full", "max_context_bytes": 5},
            context_telemetry=tel_full,
            base_sha=base_sha,
        )
        assert tel_full["context_mode"] == "full_baseline"
        assert "return 'base'" in ctx_full

    def test_baseline_falls_back_gracefully_when_ref_missing(self, tmp_path, monkeypatch, mocker):
        from gemini_review.context import build_codebase_context

        repo, _ = _init_temp_repo(tmp_path, {"README.md": "# Hi", "helper.py": "HELPER_WORKING_TREE"})
        monkeypatch.chdir(repo)
        mocker.patch("gemini_review.repo.ensure_git_ref_available", return_value=False)

        telemetry: dict = {}
        ctx = build_codebase_context(
            files=[{"filename": "main.py", "status": "modified"}],
            config={"max_context_bytes": 100_000},
            context_telemetry=telemetry,
            base_sha="nonexistent_sha",
        )
        assert telemetry.get("context_mode") == "full"
        assert not telemetry.get("baseline_included")
        assert "HELPER_WORKING_TREE" in ctx

    def test_main_baseline_integration_eliminates_duplicate_full_content(self, tmp_path, monkeypatch, mocker):
        """End-to-end main() builds base_sha prefix first and omits redundant Full Current File Content."""
        import json

        from google.genai import types

        from gemini_pr_review import main

        orig_body = "BASE_APP_LINE_1\nBASE_APP_LINE_2\n"
        repo, base_sha = _init_temp_repo(
            tmp_path,
            {
                "src/app.py": orig_body,
                "helper.py": "HELPER_BODY\n",
            },
        )
        head_body = "BASE_APP_LINE_1\nHEAD_CHANGED_LINE_2\n"
        (repo / "src" / "app.py").write_text(head_body, encoding="utf-8")
        _git(repo, "commit", "-am", "pr change")
        head_sha = _git(repo, "rev-parse", "HEAD")

        event_file = tmp_path / "event.json"
        event_file.write_text(
            json.dumps(
                {
                    "pull_request": {
                        "number": 7,
                        "head": {"sha": head_sha},
                        "base": {"sha": base_sha},
                    }
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.chdir(repo)
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_SKIP_INLINE_SUGGESTIONS", "false")
        monkeypatch.setenv("GEMINI_INCLUDE_COMMENT_HISTORY", "false")
        monkeypatch.setenv("GEMINI_PERSONA", "straight")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        mocker.patch(
            "gemini_review.get_pr_files",
            return_value=[
                {
                    "filename": "src/app.py",
                    "status": "modified",
                    "patch": "@@ -1,2 +1,2 @@\n BASE_APP_LINE_1\n-BASE_APP_LINE_2\n+HEAD_CHANGED_LINE_2",
                }
            ],
        )
        mocker.patch("gemini_review.post_review")
        mocker.patch("gemini_review.post_commit_status")

        mock_client = mocker.MagicMock()
        mock_client.models.count_tokens.return_value = mocker.Mock(total_tokens=500)
        resp = mocker.Mock()
        resp.text = json.dumps({"summary": "LGTM", "general_feedback": [], "comments": []})
        resp.candidates = [
            mocker.Mock(content=types.Content(role="model", parts=[types.Part.from_text(text=resp.text)]))
        ]
        resp.function_calls = None
        resp.usage_metadata = mocker.Mock(
            prompt_token_count=500,
            cached_content_token_count=0,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=520,
        )
        mock_client.models.generate_content.return_value = resp
        mocker.patch("google.genai.Client", return_value=mock_client)

        main()

        from gemini_pr_review import BASELINE_CONTEXT_ACK

        sent = mock_client.models.generate_content.call_args.kwargs["contents"]
        assert isinstance(sent, list)
        assert len(sent) == 3
        assert sent[0].role == "user"
        assert sent[1].role == "model"
        assert sent[1].parts[0].text == BASELINE_CONTEXT_ACK
        assert sent[2].role == "user"

        turn0_text = "\n".join(p.text for p in sent[0].parts if getattr(p, "text", None))
        turn2_text = "\n".join(p.text for p in sent[2].parts if getattr(p, "text", None))
        assert "Full Codebase Baseline" in turn0_text
        assert "HELPER_BODY" in turn0_text
        assert "--- Full Current File Content ---" not in turn2_text
        assert "Pre-PR baseline content is already included in Repository Context above." in turn2_text
        assert "+ | HEAD_CHANGED_LINE_2" in turn2_text
        assert "Pull Request Number" in turn2_text
        assert "#7" in turn2_text

        gen_cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        assert "Pull Request Number" not in gen_cfg.system_instruction

    def test_get_git_blob_rejects_empty_path(self, mocker):
        from gemini_review.repo import get_git_blob

        mock_run = mocker.patch("gemini_review.repo.subprocess.run")
        assert get_git_blob("", "HEAD") is None
        assert get_git_blob("./", "HEAD") is None
        mock_run.assert_not_called()

    def test_stage1_budget_fallback_restores_full_content_when_baseline_dropped(self, tmp_path, monkeypatch, mocker):
        """When Stage 1 drops full_baseline context, pr_diff_prompt rebuilds without stale baseline note."""
        import json

        from google.genai import types

        from gemini_pr_review import main

        repo, base_sha = _init_temp_repo(
            tmp_path,
            {
                "src/app.py": "BASE_APP_1\nBASE_APP_2\n",
                "big_background.py": "BG " * 4000,
            },
        )
        (repo / "src" / "app.py").write_text("BASE_APP_1\nHEAD_APP_UNIQUE\n", encoding="utf-8")
        _git(repo, "commit", "-am", "pr edit")
        head_sha = _git(repo, "rev-parse", "HEAD")

        event_file = tmp_path / "event.json"
        event_file.write_text(
            json.dumps(
                {
                    "pull_request": {
                        "number": 9,
                        "head": {"sha": head_sha},
                        "base": {"sha": base_sha},
                    }
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.chdir(repo)
        monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_file))
        monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_SKIP_INLINE_SUGGESTIONS", "false")
        monkeypatch.setenv("GEMINI_INCLUDE_COMMENT_HISTORY", "false")
        monkeypatch.setenv("GEMINI_PERSONA", "straight")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
        monkeypatch.setenv("GEMINI_MAX_PROMPT_TOKENS", "500")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        mocker.patch(
            "gemini_review.get_pr_files",
            return_value=[
                {
                    "filename": "src/app.py",
                    "status": "modified",
                    "patch": "@@ -1,2 +1,2 @@\n BASE_APP_1\n-BASE_APP_2\n+HEAD_APP_UNIQUE",
                }
            ],
        )
        mocker.patch("gemini_review.post_review")
        mocker.patch("gemini_review.post_commit_status")

        mock_client = mocker.MagicMock()
        # First call (with background baseline) exceeds budget=500; second call (rebuilt diff only) fits <= 500
        mock_client.models.count_tokens.side_effect = [
            mocker.Mock(total_tokens=2000),
            mocker.Mock(total_tokens=200),
        ]
        resp = mocker.Mock()
        resp.text = json.dumps({"summary": "LGTM", "general_feedback": [], "comments": []})
        resp.candidates = [
            mocker.Mock(content=types.Content(role="model", parts=[types.Part.from_text(text=resp.text)]))
        ]
        resp.function_calls = None
        resp.usage_metadata = mocker.Mock(
            prompt_token_count=200,
            cached_content_token_count=0,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=220,
        )
        mock_client.models.generate_content.return_value = resp
        mocker.patch("google.genai.Client", return_value=mock_client)

        main()

        sent = mock_client.models.generate_content.call_args.kwargs["contents"]
        assert isinstance(sent, str)
        assert "Pre-PR baseline content is already included" not in sent
        assert "--- Full Current File Content ---" in sent
        assert "HEAD_APP_UNIQUE" in sent
