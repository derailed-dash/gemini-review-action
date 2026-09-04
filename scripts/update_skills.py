# /// script
# dependencies = [
#   "rich>=13.0.0",
# ]
# ///
"""
Update Skills Utility.

This script updates and synchronises curated starter skills or consumer project
skills (.agents/skills/) from their designated public upstream Git repositories
or a local skills cache, driven declaratively by a skills-manifest.json manifest.

Why it exists:
Enables action maintainers and consuming project teams to refresh agent skills to
their latest upstream revisions without depending on local user-specific environments
or private credential caches.

How it works:
1. Auto-detects whether running in gemini-review-action (starter-examples/skills/) or
   a consumer repository (.agents/skills/) to locate skills-manifest.json.
2. Performs shallow, blob-filtered sparse checkouts into temporary scratch spaces
   to fetch only the designated skill directories quickly and with minimal bandwidth.
3. Synchronises each skill into the target directory, preserving curated catalogues
   while reporting status and file counts via Rich tables.
4. Optionally supports local directory syncing (--from-local-cache) and dry-run previewing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/derailed-dash/gemini-review-action/main/"
    "starter-examples/skills/skills-manifest.json"
)


def resolve_target_dir(repo_root: Path, explicit_target: Path | None = None) -> Path:
    """Resolve the target directory for skills synchronisation.

    If explicit_target is provided, it is returned. Otherwise:
    - In gemini-review-action repo: defaults to 'starter-examples/skills'
    - In consumer repos: defaults to '.agents/skills'

    Args:
        repo_root: Root directory of the repository.
        explicit_target: Target path specified by the user, if any.

    Returns:
        Resolved target directory path.
    """
    if explicit_target is not None:
        return explicit_target

    starter_dir = repo_root / "starter-examples" / "skills"
    if starter_dir.is_dir():
        return starter_dir

    return repo_root / ".agents" / "skills"


def resolve_manifest_path(repo_root: Path, explicit_manifest: Path | None = None) -> Path:
    """Resolve the path to the skills manifest JSON.

    If explicit_manifest is provided, it is returned. Otherwise, candidate paths
    are searched in priority order:
    1. starter-examples/skills/skills-manifest.json (action repository)
    2. .agents/skills-manifest.json (consumer repository)
    3. .agents/skills/skills-manifest.json
    4. .github/skills-manifest.json
    5. skills-manifest.json (repository root)

    Args:
        repo_root: Root directory of the repository.
        explicit_manifest: Manifest path specified by the user, if any.

    Returns:
        Resolved manifest path.
    """
    if explicit_manifest is not None:
        return explicit_manifest

    candidates = [
        repo_root / "starter-examples" / "skills" / "skills-manifest.json",
        repo_root / ".agents" / "skills-manifest.json",
        repo_root / ".agents" / "skills" / "skills-manifest.json",
        repo_root / ".github" / "skills-manifest.json",
        repo_root / "skills-manifest.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    # Fallback to standard candidate if none exist yet
    if (repo_root / "starter-examples" / "skills").is_dir():
        return candidates[0]
    return candidates[1]


def init_manifest(
    manifest_path: Path,
    repo_root: Path,
    force: bool = False,
) -> bool:
    """Initialise a skills-manifest.json file in the repository.

    Copies from starter-examples/skills/skills-manifest.json if present locally,
    or downloads the canonical manifest from the gemini-review-action main branch.

    Args:
        manifest_path: Target path where the manifest will be written.
        repo_root: Root directory of the repository.
        force: If True, overwrite an existing manifest file.

    Returns:
        True if successfully initialised, False otherwise.
    """
    if manifest_path.exists() and not force:
        console.print(f"[bold yellow]Manifest already exists:[/] {manifest_path}\n[dim]Use --force to overwrite.[/]")
        return False

    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    local_starter = repo_root / "starter-examples" / "skills" / "skills-manifest.json"
    if local_starter.is_file() and local_starter.resolve() != manifest_path.resolve():
        shutil.copyfile(local_starter, manifest_path)
        console.print(
            f"[bold green]✓ Initialised skills manifest from local template:[/] {manifest_path}\n"
            f"[dim]Run 'uv run scripts/update_skills.py' to synchronise upstream skills.[/]"
        )
        return True

    console.print(f"[cyan]Downloading default manifest from GitHub...[/]\n[dim]{DEFAULT_MANIFEST_URL}[/]")
    try:
        req = urllib.request.Request(
            DEFAULT_MANIFEST_URL,
            headers={"User-Agent": "gemini-review-action/update_skills"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read().decode("utf-8")
        json.loads(content)
        manifest_path.write_text(content, encoding="utf-8")
        console.print(
            f"[bold green]✓ Successfully downloaded and initialised skills manifest at:[/] {manifest_path}\n"
            f"[dim]Run 'uv run scripts/update_skills.py' to synchronise upstream skills.[/]"
        )
        return True
    except Exception as e:
        console.print(f"[bold red]Failed to download manifest:[/] {e}")
        return False


def load_manifest(manifest_path: Path) -> dict:
    """Load and validate the skills manifest JSON file.

    Args:
        manifest_path: Path to the JSON manifest.

    Returns:
        Dictionary containing manifest specifications.

    Raises:
        FileNotFoundError: If the manifest does not exist.
        ValueError: If required fields are missing.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"Skills manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "repositories" not in data or not isinstance(data["repositories"], list):
        raise ValueError("Invalid manifest: missing 'repositories' list.")

    return data


IGNORED_SYNC_PARTS = {".DS_Store", "__pycache__"}


def is_ignored_sync_file(path: Path) -> bool:
    """Determine whether a file path should be ignored during synchronisation.

    Ignores common OS clutter (.DS_Store) and compiled Python bytecode (__pycache__, *.pyc).
    """
    return bool(IGNORED_SYNC_PARTS & set(path.parts)) or path.name.endswith(".pyc")


def is_directory_identical(source: Path, target: Path) -> bool:
    """Check whether all files in source and target match in relative paths and content.

    Args:
        source: Source directory path.
        target: Target directory path.

    Returns:
        True if target exists, has identical relative files and identical contents to source.
    """
    if not target.is_dir():
        return False

    source_files = {p.relative_to(source): p for p in source.rglob("*") if p.is_file() and not is_ignored_sync_file(p)}
    target_files = {p.relative_to(target): p for p in target.rglob("*") if p.is_file() and not is_ignored_sync_file(p)}

    if set(source_files.keys()) != set(target_files.keys()):
        return False

    for rel_path, src_file in source_files.items():
        tgt_file = target_files[rel_path]
        if src_file.stat().st_size != tgt_file.stat().st_size:
            return False
        if src_file.read_bytes() != tgt_file.read_bytes():
            return False

    return True


def sync_skill_directory(
    source_dir: Path,
    target_dir: Path,
    skill_name: str,
    dry_run: bool = False,
) -> dict[str, str | int]:
    """Synchronise a single skill directory from source to target.

    Args:
        source_dir: Directory containing the upstream skill files.
        target_dir: Root directory where starter skills reside.
        skill_name: Name of the skill directory.
        dry_run: If True, simulate the sync without modifying disk.

    Returns:
        Dictionary with status details (status, file_count, note).
    """
    source_skill_path = source_dir / skill_name
    if not source_skill_path.exists() or not source_skill_path.is_dir():
        # Fallback: search recursively in source_dir for matching folder with SKILL.md
        matches = [p for p in source_dir.glob(f"**/{skill_name}") if p.is_dir() and (p / "SKILL.md").exists()]
        if matches:
            source_skill_path = matches[0]
        else:
            return {
                "skill": skill_name,
                "status": "failed",
                "file_count": 0,
                "note": f"Source directory not found in {source_dir}",
            }

    skill_file = source_skill_path / "SKILL.md"
    if not skill_file.exists():
        return {
            "skill": skill_name,
            "status": "warning",
            "file_count": 0,
            "note": "Missing SKILL.md in source directory",
        }

    # Count files to copy (excluding OS clutter and bytecode)
    source_files = [p for p in source_skill_path.rglob("*") if p.is_file() and not is_ignored_sync_file(p)]
    file_count = len(source_files)

    target_skill_path = target_dir / skill_name
    if is_directory_identical(source_skill_path, target_skill_path):
        return {
            "skill": skill_name,
            "status": "up-to-date",
            "file_count": file_count,
            "note": "Already up to date",
        }

    if dry_run:
        return {
            "skill": skill_name,
            "status": "simulated",
            "file_count": file_count,
            "note": f"Would sync {file_count} files (differences detected)",
        }

    try:
        if target_skill_path.exists():
            shutil.rmtree(target_skill_path)
        shutil.copytree(
            source_skill_path,
            target_skill_path,
            ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc"),
        )
        return {
            "skill": skill_name,
            "status": "updated",
            "file_count": file_count,
            "note": f"Synchronised {file_count} files",
        }
    except Exception as e:
        return {
            "skill": skill_name,
            "status": "error",
            "file_count": 0,
            "note": f"Copy failed: {e}",
        }


def update_from_local_cache(
    cache_path: Path,
    target_dir: Path,
    manifest: dict,
    skill_filter: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, str | int]]:
    """Update skills using an existing local directory cache.

    Args:
        cache_path: Directory containing pre-installed skills.
        target_dir: Root starter-examples/skills directory.
        manifest: Loaded manifest dictionary.
        skill_filter: Optional single skill name to filter by.
        dry_run: If True, do not modify target directory.

    Returns:
        List of result dictionaries.
    """
    results: list[dict[str, str | int]] = []

    for repo in manifest.get("repositories", []):
        for skill_name in repo.get("skills", []):
            if skill_filter and skill_name != skill_filter:
                continue

            result = sync_skill_directory(
                source_dir=cache_path,
                target_dir=target_dir,
                skill_name=skill_name,
                dry_run=dry_run,
            )
            result["repo"] = repo.get("name", "unknown")
            results.append(result)

    return results


def fetch_and_sync_remote_repo(
    repo_info: dict,
    target_dir: Path,
    skill_filter: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, str | int]]:
    """Clone sparse tree of an upstream Git repository and synchronise its skills.

    Args:
        repo_info: Repository configuration dictionary from manifest.
        target_dir: Destination starter-examples/skills directory.
        skill_filter: Optional skill name filter.
        dry_run: If True, simulate operations.

    Returns:
        List of result dictionaries.
    """
    repo_name = repo_info.get("name", "unknown")
    url = repo_info["url"]
    branch = repo_info.get("branch", "main")
    skills_root = repo_info.get("skills_root", "skills").strip("/")
    skills = repo_info.get("skills", [])

    target_skills = [s for s in skills if not skill_filter or s == skill_filter]
    if not target_skills:
        return []

    console.print(f"[bold cyan]Fetching {repo_name}[/] from [underline]{url}[/] ({branch})...")

    results: list[dict[str, str | int]] = []

    with tempfile.TemporaryDirectory(prefix="skills_sync_") as temp_dir:
        temp_path = Path(temp_dir)

        # 1. Shallow sparse clone without checking out files
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--filter=blob:none",
            "--no-checkout",
            "--branch",
            branch,
            url,
            str(temp_path),
        ]
        clone_proc = subprocess.run(clone_cmd, capture_output=True, text=True)
        if clone_proc.returncode != 0:
            console.print(f"[bold red]Failed to clone {repo_name}:[/] {clone_proc.stderr.strip()}")
            for skill_name in target_skills:
                results.append(
                    {
                        "skill": skill_name,
                        "repo": repo_name,
                        "status": "error",
                        "file_count": 0,
                        "note": f"Git clone failed: {clone_proc.stderr.strip()}",
                    }
                )
            return results

        # 2. Configure sparse checkout for target skill folders
        sparse_patterns = [f"{skills_root}/{s}" for s in target_skills]
        sparse_cmd = ["git", "-C", str(temp_path), "sparse-checkout", "set"] + sparse_patterns
        sparse_proc = subprocess.run(sparse_cmd, capture_output=True, text=True)
        if sparse_proc.returncode != 0:
            console.print(f"[bold red]Failed to set sparse-checkout for {repo_name}:[/] {sparse_proc.stderr.strip()}")
            for skill_name in target_skills:
                results.append(
                    {
                        "skill": skill_name,
                        "repo": repo_name,
                        "status": "error",
                        "file_count": 0,
                        "note": f"Sparse checkout failed: {sparse_proc.stderr.strip()}",
                    }
                )
            return results

        # 3. Checkout branch files for the sparse selection
        checkout_cmd = ["git", "-C", str(temp_path), "checkout", branch]
        checkout_proc = subprocess.run(checkout_cmd, capture_output=True, text=True)
        if checkout_proc.returncode != 0:
            console.print(f"[bold red]Failed to checkout {branch} in {repo_name}:[/] {checkout_proc.stderr.strip()}")
            for skill_name in target_skills:
                results.append(
                    {
                        "skill": skill_name,
                        "repo": repo_name,
                        "status": "error",
                        "file_count": 0,
                        "note": f"Checkout failed: {checkout_proc.stderr.strip()}",
                    }
                )
            return results

        # 4. Synchronise each skill from the sparse tree
        source_root = temp_path / skills_root if skills_root else temp_path
        for skill_name in target_skills:
            res = sync_skill_directory(
                source_dir=source_root,
                target_dir=target_dir,
                skill_name=skill_name,
                dry_run=dry_run,
            )
            res["repo"] = repo_name
            results.append(res)

    return results


def display_results(results: list[dict[str, str | int]], dry_run: bool = False) -> None:
    """Format and print a Rich table summary of synchronisation outcomes.

    Args:
        results: List of skill sync result dictionaries.
        dry_run: Whether execution was a dry-run simulation.
    """
    title = (
        "[bold yellow]Starter Skills Update Summary (Simulation)[/]"
        if dry_run
        else "[bold green]Starter Skills Update Summary[/]"
    )
    table = Table(title=title, show_lines=True)
    table.add_column("Skill Name", style="cyan", no_wrap=True)
    table.add_column("Upstream Repository", style="magenta")
    table.add_column("Status", style="bold")
    table.add_column("Files", justify="right")
    table.add_column("Notes", style="italic")

    status_styles = {
        "up-to-date": "[green]Up to date[/]",
        "updated": "[green]Updated[/]",
        "simulated": "[yellow]Would sync[/]",
        "warning": "[yellow]Warning[/]",
        "failed": "[red]Failed[/]",
        "error": "[red]Error[/]",
    }

    for res in results:
        status_text = status_styles.get(str(res.get("status")), str(res.get("status")))
        table.add_row(
            str(res.get("skill")),
            str(res.get("repo")),
            status_text,
            str(res.get("file_count")),
            str(res.get("note")),
        )

    console.print(table)


def main() -> int:
    """Entrypoint for the starter skills updater CLI."""
    parser = argparse.ArgumentParser(
        description="Update starter or project skills from upstream Git repositories or a local cache."
    )
    repo_root = Path(__file__).resolve().parent.parent

    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to skills manifest JSON (default: auto-detected)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=None,
        help="Target directory for skills (default: auto-detected)",
    )
    parser.add_argument(
        "--from-local-cache",
        type=Path,
        default=None,
        help="Optional local directory containing skills to copy directly without git network calls.",
    )
    parser.add_argument(
        "--skill",
        type=str,
        default=None,
        help="Optional single skill name to update.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Optional repository name filter (e.g. 'google/skills').",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Initialise default skills-manifest.json (copied from starter-examples or downloaded from GitHub).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing skills-manifest.json when running with --init.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the update process without modifying target directories.",
    )

    args = parser.parse_args()

    manifest_path = resolve_manifest_path(repo_root, args.manifest)
    target_dir = resolve_target_dir(repo_root, args.target_dir)

    if args.init:
        return 0 if init_manifest(manifest_path, repo_root, force=args.force) else 1

    is_action_repo = (repo_root / "starter-examples" / "skills").is_dir()
    repo_type_label = (
        "Action Repository (starter-examples)" if is_action_repo else "Consumer Repository (.agents/skills)"
    )

    header_text = (
        "[bold cyan]Gemini Review Action — Skills Synchroniser[/]\n"
        f"Mode:     [cyan]{repo_type_label}[/]\n"
        f"Manifest: [dim]{manifest_path}[/]\n"
        f"Target:   [dim]{target_dir}[/]"
    )
    if args.dry_run:
        header_text += "\n[bold yellow]Mode: DRY-RUN (no files will be written)[/]"
    console.print(Panel(header_text, expand=False))

    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        console.print(
            f"[bold red]Skills manifest not found:[/] {manifest_path}\n"
            "[yellow]To initialise the default curated manifest, run:\n"
            "  uv run scripts/update_skills.py --init\n"
            "Or copy 'starter-examples/skills/skills-manifest.json' from gemini-review-action.[/]"
        )
        return 1
    except Exception as e:
        console.print(f"[bold red]Error loading manifest:[/] {e}")
        return 1

    if not args.dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, str | int]] = []

    if args.from_local_cache:
        cache_path = args.from_local_cache.expanduser().resolve()
        if not cache_path.exists():
            console.print(f"[bold red]Local cache directory not found:[/] {cache_path}")
            return 1
        console.print(f"[cyan]Syncing from local cache:[/] {cache_path}")
        results = update_from_local_cache(
            cache_path=cache_path,
            target_dir=target_dir,
            manifest=manifest,
            skill_filter=args.skill,
            dry_run=args.dry_run,
        )
    else:
        for repo_info in manifest.get("repositories", []):
            repo_name = repo_info.get("name")
            if args.repo and repo_name != args.repo:
                continue
            repo_results = fetch_and_sync_remote_repo(
                repo_info=repo_info,
                target_dir=target_dir,
                skill_filter=args.skill,
                dry_run=args.dry_run,
            )
            results.extend(repo_results)

    display_results(results, dry_run=args.dry_run)

    # Note local skills
    for local_skill in manifest.get("local_skills", []):
        name = local_skill.get("name")
        desc = local_skill.get("description")
        console.print(f"[dim]ℹ️ Note: '{name}' is maintained directly in this repository ({desc}).[/]")

    has_errors = any(res.get("status") in ("error", "failed") for res in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
