"""Repository filesystem traversal, Git tree operations, and candidate ranking heuristics.

This module provides tools for discovering tracked text files, generating ASCII file trees,
matching core repository documentation, parsing import references, and bounding candidates
for dynamic context selection.
"""

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath


def is_text_file(filename: str) -> bool:
    """Filter out typical binary, lock, media, minified, and encrypted file formats."""
    excluded_extensions = {
        # Images & Vectors
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".pdf",
        # Archives & Packages
        ".zip",
        ".tar",
        ".gz",
        ".7z",
        ".rar",
        ".whl",
        ".jar",
        ".war",
        ".ear",
        # Encrypted, Lock, DB, Compiled
        ".enc",
        ".lock",
        ".db",
        ".pyc",
        ".o",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        # Fonts
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
        ".otf",
        # Media & Audio / Video
        ".mp4",
        ".mp3",
        ".wav",
        ".mov",
        ".avi",
        ".webm",
        ".flac",
        ".ogg",
        ".mkv",
        # Source maps
        ".map",
        # Test snapshots
        ".snap",
        ".snapshot",
        # Data & ML artifacts
        ".parquet",
        ".arrow",
        ".h5",
        ".hdf5",
        ".pkl",
        ".pickle",
        ".onnx",
        ".pt",
        ".pth",
        ".bin",
    }
    fname_lower = filename.lower()
    _, ext = os.path.splitext(fname_lower)
    if ext in excluded_extensions:
        return False

    if fname_lower.endswith(".min.js") or fname_lower.endswith(".min.css") or fname_lower.endswith(".bundle.js"):
        return False

    excluded_names = {
        # Dependency lockfiles
        "package-lock.json",
        "uv.lock",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "bun.lock",
        "cargo.lock",
        "poetry.lock",
        "pipfile.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "packages.lock.json",
        "flake.lock",
        "pdm.lock",
        # Secrets & environment
        ".env",
        ".env.enc",
        ".envrc",
        # System & OS metadata
        ".ds_store",
        "thumbs.db",
    }
    if os.path.basename(filename).lower() in excluded_names:
        return False

    return True


def get_file_content(path: str) -> str:
    """Read file content safely as UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def get_local_git_files() -> list:
    """Developer fallback to gather file diffs from local git tree."""
    try:
        res = subprocess.run(["git", "diff", "main...HEAD", "--name-only"], capture_output=True, text=True, check=True)
        filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]
        diff_base = "main...HEAD"

        if not filenames:
            # Fall back to uncommitted/staged working tree changes vs HEAD
            res = subprocess.run(["git", "diff", "HEAD", "--name-only"], capture_output=True, text=True, check=True)
            filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]
            diff_base = "HEAD"

        files = []
        for filename in filenames:
            diff_res = subprocess.run(
                ["git", "diff", diff_base, "--", filename], capture_output=True, text=True, check=True
            )
            files.append({"filename": filename, "status": "modified", "patch": diff_res.stdout})
        return files

    except Exception as e:
        print(f"Error running local git diff: {e}", file=sys.stderr)
        return []


def get_git_blob(file_path: str, ref: str = "HEAD") -> bytes | None:
    """Retrieve raw file content bytes from git for a given ref (e.g. branch, tag, or SHA)."""
    norm_path = file_path.replace("\\", "/").removeprefix("./")
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{norm_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout
    except Exception:
        return None


def get_all_repo_files() -> list[str]:
    """Get list of all tracked text files in the repository."""
    from gemini_review.utils import _get_pr_review_func

    fn_is_text_file = _get_pr_review_func("is_text_file", is_text_file)
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
        all_files = [f.strip() for f in res.stdout.split("\n") if f.strip()]
        return [f.replace("\\", "/") for f in all_files if fn_is_text_file(f) and os.path.exists(f)]
    except Exception as e:
        print(f"Error running git ls-files: {e}", file=sys.stderr)
        # Fallback to os.walk if git is not available
        text_files = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                filepath = os.path.relpath(os.path.join(root, file), ".")
                if fn_is_text_file(filepath) and os.path.exists(filepath):
                    text_files.append(filepath.replace("\\", "/"))
        return text_files


def is_core_file(filename: str, patterns: list[str]) -> bool:
    """Check if the filename matches any of the core file patterns (case-insensitive)."""
    norm_path = filename.replace("\\", "/").removeprefix("./")
    basename = os.path.basename(norm_path)
    posix_path = PurePosixPath(norm_path.lower())

    for pattern in patterns:
        norm_pat = pattern.replace("\\", "/").removeprefix("./").lower()
        if "/" in norm_pat:
            if posix_path.match(norm_pat):
                return True
        else:
            if (
                fnmatch.fnmatch(basename.lower(), norm_pat)
                or fnmatch.fnmatch(norm_path.lower(), norm_pat)
                or posix_path.match(norm_pat)
            ):
                return True
    return False


def generate_file_tree(files: list[str]) -> str:
    """Generate a text-based folder tree structure from a list of file paths."""
    tree = {}
    for f in sorted(files):
        parts = f.replace("\\", "/").split("/")
        curr = tree
        for part in parts:
            if part not in curr:
                curr[part] = {}
            curr = curr[part]

    def _render(node: dict, indent: str = "") -> list[str]:
        lines = []
        keys = list(node.keys())
        for idx, key in enumerate(keys):
            is_last = idx == len(keys) - 1
            marker = "└── " if is_last else "├── "
            child_indent = "    " if is_last else "│   "
            if node[key]:
                lines.append(f"{indent}{marker}{key}/")
                lines.extend(_render(node[key], indent + child_indent))
            else:
                lines.append(f"{indent}{marker}{key}")
        return lines

    return ".\n" + "\n".join(_render(tree))


def extract_import_references(files: list[dict]) -> set[str]:
    """Extract referenced modules, package names, and file basenames from modified diff patches."""
    refs: set[str] = set()
    if not files:
        return refs

    # Patterns matching import/require/include across Python, JS/TS, Go, C/C++, Rust
    patterns = [
        # Python: from foo.bar import baz, import foo.bar
        r"^\+?\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))",
        # JS/TS: import ... from './foo' or require('./foo')
        r"^\+?\s*(?:import\s+(?:(?:\{[^}]*\}|\*\s+as\s+\w+|\w+)\s+from\s+)?[\"']([^\"']+)[\"']|require\([\"']([^\"']+)[\"']\))",
        # Go: import "foo/bar" or inside import block
        r"^\+?\s*[\"']([a-zA-Z0-9_./\-]+)[\"']",
        # C/C++: #include "foo/bar.h" or <foo/bar.h>
        r"^\+?\s*#\s*include\s*[<\"]([^>\"]+)[>\"]",
        # Rust: use foo::bar;
        r"^\+?\s*use\s+([a-zA-Z0-9_:]+)",
    ]
    compiled = [re.compile(p, re.MULTILINE) for p in patterns]

    for f in files:
        patch = f.get("patch") or ""
        if not patch:
            continue
        for cp in compiled:
            for match in cp.finditer(patch):
                for group in match.groups():
                    if not group:
                        continue
                    cleaned = group.strip().replace("::", "/")
                    if "/" not in cleaned:
                        parts = cleaned.split(".")
                        base = parts[-1]
                        cleaned = "/".join(parts)
                    else:
                        norm = cleaned.lstrip("./").lstrip("/")
                        base = os.path.splitext(os.path.basename(cleaned))[0]
                        cleaned = os.path.splitext(norm)[0]

                    # Extract module basename (e.g. 'tokens' from 'src.auth.tokens')
                    if base and len(base) > 1 and not base.isdigit():
                        refs.add(base)
                        refs.add(base.lower())
                    # Also include full import path reference
                    if "/" in cleaned:
                        refs.add(cleaned)
                        refs.add(cleaned.lower())

    return refs


def rank_and_bound_candidates(
    candidate_files: list[str],
    modified_files: list[dict],
    max_candidates: int = 500,
    diff_dirs_only: bool = False,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """Score, prioritise, and bound candidate context files using directory proximity and import heuristics."""
    if not candidate_files:
        return []

    from gemini_review.utils import _get_pr_review_func

    fn_is_text = _get_pr_review_func("is_text_file", is_text_file)
    candidate_files = [c for c in candidate_files if fn_is_text(c)]

    # Filter out candidates matching explicit exclude patterns
    if exclude_patterns:
        filtered = []
        for c in candidate_files:
            c_posix = PurePosixPath(c.replace("\\", "/").removeprefix("./"))
            matched = False
            for pat in exclude_patterns:
                norm_pat = pat.replace("\\", "/").removeprefix("./")
                if "/" in norm_pat:
                    if c_posix.match(norm_pat):
                        matched = True
                        break
                else:
                    if fnmatch.fnmatch(c_posix.name, norm_pat) or c_posix.match(norm_pat):
                        matched = True
                        break
            if not matched:
                filtered.append(c)
        candidate_files = filtered

    modified_paths = [
        f.get("filename", "").replace("\\", "/").removeprefix("./") for f in modified_files if f.get("filename")
    ]
    diff_dirs = {os.path.dirname(p) for p in modified_paths}
    diff_basenames = {os.path.splitext(os.path.basename(p))[0].lower() for p in modified_paths}
    diff_extensions = {os.path.splitext(p)[1].lower() for p in modified_paths if os.path.splitext(p)[1]}
    import_refs = extract_import_references(modified_files)

    if diff_dirs_only:
        scoped = []
        for c in candidate_files:
            c_norm = c.replace("\\", "/").removeprefix("./")
            c_dir = os.path.dirname(c_norm)
            if c_dir in diff_dirs or any(c_dir.startswith(d + "/") for d in diff_dirs if d):
                scoped.append(c)
        candidate_files = scoped

    if len(candidate_files) <= max_candidates and not diff_dirs_only:
        # If candidate count is already within budget, maintain original order
        return candidate_files

    # Score candidates
    scored: list[tuple[int, str]] = []
    for c in candidate_files:
        c_norm = c.replace("\\", "/").removeprefix("./")
        c_dir = os.path.dirname(c_norm)
        c_base = os.path.splitext(os.path.basename(c_norm))[0].lower()
        c_ext = os.path.splitext(c_norm)[1].lower()

        score = 0
        # 1. Exact module/symbol import match (highest priority)
        if c_base in import_refs:
            score += 120
        elif any(c_norm.lower().endswith(ref) or ref == c_norm.lower() for ref in import_refs if "/" in ref):
            score += 100

        # 2. Sister or test file match (e.g. test_login.py for login.py)
        if any(base in c_base for base in diff_basenames if len(base) > 2):
            score += 80

        # 3. Exact same directory as modified file
        if c_dir in diff_dirs:
            score += 60
        # 4. Immediate parent or child directory of modified file
        elif any(c_dir.startswith(d + "/") or (d and d.startswith(c_dir + "/")) for d in diff_dirs):
            score += 40

        # 5. Shared file extension
        if c_ext in diff_extensions:
            score += 20

        scored.append((score, c))

    # Sort descending by score, ascending by path for stability
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [c for _, c in scored[:max_candidates]]
