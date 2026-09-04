"""
Unit tests for the Skills Updater script (scripts/update_skills.py).

Tests manifest parsing, local cache synchronisation, sparse git checkout handling,
path resolution across repository modes, dry-run previewing, and error resilience.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.update_skills import (
    fetch_and_sync_remote_repo,
    init_manifest,
    is_directory_identical,
    load_manifest,
    resolve_manifest_path,
    resolve_target_dir,
    sync_skill_directory,
    update_from_local_cache,
)


@pytest.fixture
def sample_manifest_path(tmp_path: Path) -> Path:
    manifest_data = {
        "version": "1.0",
        "repositories": [
            {
                "name": "google/skills",
                "url": "https://github.com/google/skills.git",
                "branch": "main",
                "skills_root": "skills",
                "skills": ["cloud-run-basics", "gke-basics"],
            },
            {
                "name": "google-gemini/gemini-skills",
                "url": "https://github.com/google-gemini/gemini-skills.git",
                "branch": "main",
                "skills_root": "skills",
                "skills": ["gemini-api-dev"],
            },
        ],
        "local_skills": [
            {"name": "bigquery", "description": "Local composite skill"},
        ],
    }
    manifest_file = tmp_path / "skills-manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")
    return manifest_file


class TestLoadManifest:
    def test_load_manifest_valid(self, sample_manifest_path: Path):
        data = load_manifest(sample_manifest_path)
        assert "repositories" in data
        assert len(data["repositories"]) == 2
        assert data["repositories"][0]["name"] == "google/skills"

    def test_load_manifest_nonexistent(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.json"
        with pytest.raises(FileNotFoundError, match="Skills manifest not found"):
            load_manifest(missing)

    def test_load_manifest_invalid_json(self, tmp_path: Path):
        bad_file = tmp_path / "invalid.json"
        bad_file.write_text("not json content", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_manifest(bad_file)

    def test_load_manifest_missing_repositories(self, tmp_path: Path):
        empty_manifest = tmp_path / "empty.json"
        empty_manifest.write_text(json.dumps({"version": "1.0"}), encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'repositories' list"):
            load_manifest(empty_manifest)


class TestSyncSkillDirectory:
    def test_sync_skill_directory_missing_source(self, tmp_path: Path):
        source_dir = tmp_path / "source"
        target_dir = tmp_path / "target"
        result = sync_skill_directory(source_dir, target_dir, "unknown-skill")
        assert result["status"] == "failed"
        assert result["file_count"] == 0

    def test_sync_skill_directory_missing_skill_md(self, tmp_path: Path):
        source_skill = tmp_path / "source" / "my-skill"
        source_skill.mkdir(parents=True)
        (source_skill / "helper.py").write_text("# helper", encoding="utf-8")

        target_dir = tmp_path / "target"
        result = sync_skill_directory(tmp_path / "source", target_dir, "my-skill")
        assert result["status"] == "warning"
        assert "Missing SKILL.md" in str(result["note"])

    def test_sync_skill_directory_dry_run(self, tmp_path: Path):
        source_skill = tmp_path / "source" / "my-skill"
        source_skill.mkdir(parents=True)
        (source_skill / "SKILL.md").write_text("# My Skill", encoding="utf-8")
        (source_skill / "notes.txt").write_text("notes", encoding="utf-8")

        target_dir = tmp_path / "target"
        result = sync_skill_directory(tmp_path / "source", target_dir, "my-skill", dry_run=True)
        assert result["status"] == "simulated"
        assert result["file_count"] == 2
        assert not (target_dir / "my-skill").exists()

    def test_sync_skill_directory_success(self, tmp_path: Path):
        source_skill = tmp_path / "source" / "my-skill"
        source_skill.mkdir(parents=True)
        (source_skill / "SKILL.md").write_text("# New Version", encoding="utf-8")

        target_skill = tmp_path / "target" / "my-skill"
        target_skill.mkdir(parents=True)
        (target_skill / "SKILL.md").write_text("# Old Version", encoding="utf-8")
        (target_skill / "stale.txt").write_text("obsolete", encoding="utf-8")

        target_dir = tmp_path / "target"
        result = sync_skill_directory(tmp_path / "source", target_dir, "my-skill", dry_run=False)
        assert result["status"] == "updated"
        assert result["file_count"] == 1
        assert (target_skill / "SKILL.md").read_text(encoding="utf-8") == "# New Version"
        assert not (target_skill / "stale.txt").exists()

    def test_sync_skill_directory_already_up_to_date(self, tmp_path: Path):
        source_skill = tmp_path / "source" / "my-skill"
        source_skill.mkdir(parents=True)
        (source_skill / "SKILL.md").write_text("# Same Content", encoding="utf-8")

        target_skill = tmp_path / "target" / "my-skill"
        target_skill.mkdir(parents=True)
        (target_skill / "SKILL.md").write_text("# Same Content", encoding="utf-8")

        # Test dry-run: should report up-to-date
        result_dry = sync_skill_directory(tmp_path / "source", tmp_path / "target", "my-skill", dry_run=True)
        assert result_dry["status"] == "up-to-date"
        assert "Already up to date" in str(result_dry["note"])

        # Test real run: should report up-to-date without rewriting
        result_real = sync_skill_directory(tmp_path / "source", tmp_path / "target", "my-skill", dry_run=False)
        assert result_real["status"] == "up-to-date"
        assert "Already up to date" in str(result_real["note"])

    def test_is_directory_identical(self, tmp_path: Path):
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        (dir_a / "f1.txt").write_text("hello", encoding="utf-8")
        (dir_b / "f1.txt").write_text("hello", encoding="utf-8")
        assert is_directory_identical(dir_a, dir_b) is True

        (dir_b / "f1.txt").write_text("changed", encoding="utf-8")
        assert is_directory_identical(dir_a, dir_b) is False

        (dir_b / "f1.txt").write_text("hello", encoding="utf-8")
        (dir_b / "f2.txt").write_text("extra", encoding="utf-8")
        assert is_directory_identical(dir_a, dir_b) is False

        # Clutter (.DS_Store, __pycache__, *.pyc) should be ignored during equality check
        (dir_b / "f2.txt").unlink()
        (dir_b / ".DS_Store").write_bytes(b"\x00\x00")
        pycache_dir = dir_b / "__pycache__"
        pycache_dir.mkdir()
        (pycache_dir / "helper.cpython-313.pyc").write_bytes(b"\x00\x01")
        (dir_b / "cached.pyc").write_bytes(b"\x00\x02")
        assert is_directory_identical(dir_a, dir_b) is True


class TestUpdateFromLocalCache:
    def test_update_from_local_cache_all(self, tmp_path: Path, sample_manifest_path: Path):
        cache_dir = tmp_path / "cache"
        for name in ["cloud-run-basics", "gke-basics", "gemini-api-dev"]:
            skill_folder = cache_dir / name
            skill_folder.mkdir(parents=True)
            (skill_folder / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        manifest = load_manifest(sample_manifest_path)
        target_dir = tmp_path / "starter-skills"

        results = update_from_local_cache(cache_dir, target_dir, manifest, dry_run=False)
        assert len(results) == 3
        for res in results:
            assert res["status"] == "updated"
            assert (target_dir / str(res["skill"]) / "SKILL.md").exists()

    def test_update_from_local_cache_filtered(self, tmp_path: Path, sample_manifest_path: Path):
        cache_dir = tmp_path / "cache"
        for name in ["cloud-run-basics", "gke-basics", "gemini-api-dev"]:
            skill_folder = cache_dir / name
            skill_folder.mkdir(parents=True)
            (skill_folder / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

        manifest = load_manifest(sample_manifest_path)
        target_dir = tmp_path / "starter-skills"

        results = update_from_local_cache(
            cache_dir,
            target_dir,
            manifest,
            skill_filter="cloud-run-basics",
            dry_run=False,
        )
        assert len(results) == 1
        assert results[0]["skill"] == "cloud-run-basics"
        assert (target_dir / "cloud-run-basics" / "SKILL.md").exists()
        assert not (target_dir / "gke-basics").exists()


class TestFetchAndSyncRemoteRepo:
    def test_fetch_and_sync_clone_failure(self, tmp_path: Path):
        repo_info = {
            "name": "google/skills",
            "url": "https://github.com/google/skills.git",
            "branch": "main",
            "skills_root": "skills",
            "skills": ["cloud-run-basics"],
        }
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 128
            mock_proc.stderr = "Repository not found"
            mock_run.return_value = mock_proc

            results = fetch_and_sync_remote_repo(repo_info, tmp_path / "target")
            assert len(results) == 1
            assert results[0]["status"] == "error"
            assert "Git clone failed" in str(results[0]["note"])

    def test_fetch_and_sync_sparse_failure(self, tmp_path: Path):
        repo_info = {
            "name": "google/skills",
            "url": "https://github.com/google/skills.git",
            "branch": "main",
            "skills_root": "skills",
            "skills": ["cloud-run-basics"],
        }
        with patch("subprocess.run") as mock_run:
            # Clone succeeds, sparse-checkout fails
            mock_clone = MagicMock(returncode=0)
            mock_sparse = MagicMock(returncode=1, stderr="sparse error")
            mock_run.side_effect = [mock_clone, mock_sparse]

            results = fetch_and_sync_remote_repo(repo_info, tmp_path / "target")
            assert len(results) == 1
            assert results[0]["status"] == "error"
            assert "Sparse checkout failed" in str(results[0]["note"])

    def test_fetch_and_sync_checkout_failure(self, tmp_path: Path):
        repo_info = {
            "name": "google/skills",
            "url": "https://github.com/google/skills.git",
            "branch": "main",
            "skills_root": "skills",
            "skills": ["cloud-run-basics"],
        }
        with patch("subprocess.run") as mock_run:
            mock_clone = MagicMock(returncode=0)
            mock_sparse = MagicMock(returncode=0)
            mock_checkout = MagicMock(returncode=1, stderr="checkout error")
            mock_run.side_effect = [mock_clone, mock_sparse, mock_checkout]

            results = fetch_and_sync_remote_repo(repo_info, tmp_path / "target")
            assert len(results) == 1
            assert results[0]["status"] == "error"
            assert "Checkout failed" in str(results[0]["note"])

    def test_fetch_and_sync_filter_skips_unmatched_repo(self, tmp_path: Path):
        repo_info = {
            "name": "google/skills",
            "url": "https://github.com/google/skills.git",
            "skills": ["cloud-run-basics"],
        }
        results = fetch_and_sync_remote_repo(
            repo_info,
            tmp_path / "target",
            skill_filter="different-skill",
        )
        assert results == []


class TestPathResolution:
    def test_resolve_target_dir_explicit(self, tmp_path: Path):
        explicit = tmp_path / "custom" / "skills"
        resolved = resolve_target_dir(tmp_path, explicit_target=explicit)
        assert resolved == explicit

    def test_resolve_target_dir_action_repo(self, tmp_path: Path):
        starter_dir = tmp_path / "starter-examples" / "skills"
        starter_dir.mkdir(parents=True)
        resolved = resolve_target_dir(tmp_path)
        assert resolved == starter_dir

    def test_resolve_target_dir_consumer_repo(self, tmp_path: Path):
        # When starter-examples/skills does not exist
        resolved = resolve_target_dir(tmp_path)
        assert resolved == tmp_path / ".agents" / "skills"

    def test_resolve_manifest_path_explicit(self, tmp_path: Path):
        explicit = tmp_path / "custom-manifest.json"
        resolved = resolve_manifest_path(tmp_path, explicit_manifest=explicit)
        assert resolved == explicit

    def test_resolve_manifest_path_action_repo(self, tmp_path: Path):
        action_manifest = tmp_path / "starter-examples" / "skills" / "skills-manifest.json"
        action_manifest.parent.mkdir(parents=True)
        action_manifest.write_text("{}", encoding="utf-8")
        resolved = resolve_manifest_path(tmp_path)
        assert resolved == action_manifest

    def test_resolve_manifest_path_consumer_repo_existing(self, tmp_path: Path):
        agents_manifest = tmp_path / ".agents" / "skills-manifest.json"
        agents_manifest.parent.mkdir(parents=True)
        agents_manifest.write_text("{}", encoding="utf-8")
        resolved = resolve_manifest_path(tmp_path)
        assert resolved == agents_manifest

    def test_resolve_manifest_path_consumer_repo_fallback(self, tmp_path: Path):
        # No manifest exists anywhere and starter-examples does not exist
        resolved = resolve_manifest_path(tmp_path)
        assert resolved == tmp_path / ".agents" / "skills-manifest.json"


class TestInitManifest:
    def test_init_manifest_from_local_template(self, tmp_path: Path):
        repo_root = tmp_path / "repo"
        starter = repo_root / "starter-examples" / "skills" / "skills-manifest.json"
        starter.parent.mkdir(parents=True)
        starter.write_text('{"version": "1.0", "repositories": []}', encoding="utf-8")

        target_manifest = repo_root / ".agents" / "skills-manifest.json"
        success = init_manifest(target_manifest, repo_root)
        assert success is True
        assert target_manifest.exists()
        assert json.loads(target_manifest.read_text(encoding="utf-8"))["version"] == "1.0"

    def test_init_manifest_already_exists_no_force(self, tmp_path: Path):
        repo_root = tmp_path / "repo"
        target_manifest = repo_root / ".agents" / "skills-manifest.json"
        target_manifest.parent.mkdir(parents=True)
        target_manifest.write_text("existing", encoding="utf-8")

        success = init_manifest(target_manifest, repo_root, force=False)
        assert success is False
        assert target_manifest.read_text(encoding="utf-8") == "existing"

        # Overwrite with force
        starter = repo_root / "starter-examples" / "skills" / "skills-manifest.json"
        starter.parent.mkdir(parents=True)
        starter.write_text("overwritten", encoding="utf-8")

        success_force = init_manifest(target_manifest, repo_root, force=True)
        assert success_force is True
        assert target_manifest.read_text(encoding="utf-8") == "overwritten"

    @patch("urllib.request.urlopen")
    def test_init_manifest_download(self, mock_urlopen: MagicMock, tmp_path: Path):
        repo_root = tmp_path / "repo"
        target_manifest = repo_root / ".agents" / "skills-manifest.json"

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"version": "1.0", "repositories": []}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        success = init_manifest(target_manifest, repo_root)
        assert success is True
        assert target_manifest.exists()
        assert json.loads(target_manifest.read_text(encoding="utf-8"))["version"] == "1.0"

    @patch("urllib.request.urlopen")
    def test_init_manifest_download_failure(self, mock_urlopen: MagicMock, tmp_path: Path):
        repo_root = tmp_path / "repo"
        target_manifest = repo_root / ".agents" / "skills-manifest.json"

        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        success = init_manifest(target_manifest, repo_root)
        assert success is False
        assert not target_manifest.exists()
