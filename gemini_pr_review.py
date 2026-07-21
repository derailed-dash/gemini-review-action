# /// script
# dependencies = [
#   "google-genai>=2.12.1",
#   "requests",
#   "pydantic",
# ]
# ///
#!/usr/bin/env python3
"""
Description: Runs a Pull Request code review using the Google GenAI SDK.
Supports both standard PR events and comment-triggered '/gemini-review' runs.
Includes dry-run mode for local developers to test and run offline.

Outputs and logs (including errors and progress messages) are printed to stderr
and stdout, which are viewable in the GitHub Actions runner execution logs
for the workflow run.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib

import requests
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

DEFAULT_TIMEOUT = 60


class InlineComment(BaseModel):
    """Represents a single inline comment to be posted on a file in the Pull Request."""

    path: str = Field(description="The relative file path being reviewed.")
    line: int = Field(
        description="The line number in the RIGHT (new/modified) version of the file where the comment applies."
    )
    side: str = Field(
        default="RIGHT", description="Must be 'RIGHT' for additions/modifications or 'LEFT' for deletions."
    )
    severity: str = Field(description="Severity icon: 🔴 (Critical), 🟠 (High), 🟡 (Medium), 🟢 (Low)")
    comment_text: str = Field(
        description="Constructive feedback explaining the issue. Write the feedback comments in the requested language."
    )
    code_suggestion: str | None = Field(
        None,
        description=(
            "Optional drop-in code suggestion replacement. Must match the exact structure and indentation of the"
            " replaced code, formatted as a suggestion."
        ),
    )


class ReviewResult(BaseModel):
    """Represents the structured review results returned by the Gemini model."""

    summary: str = Field(
        description="A brief, high-level assessment of the Pull Request's objective and quality (2-3 sentences)."
    )
    general_feedback: list[str] = Field(
        description="General feedback items, positive observations, or non-line-specific feedback."
    )
    comments: list[InlineComment] = Field(description="Line-specific code review comments and suggestions.")


def is_text_file(filename: str) -> bool:
    """Filter out typical binary, lock, and encrypted file formats."""
    excluded_extensions = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".svg",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".enc",
        ".lock",
        ".db",
        ".pyc",
        ".o",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".woff",
        ".woff2",
        ".eot",
        ".ttf",
    }
    _, ext = os.path.splitext(filename.lower())
    if ext in excluded_extensions:
        return False

    excluded_names = {"package-lock.json", "uv.lock", ".env", ".env.enc", ".envrc"}
    if os.path.basename(filename) in excluded_names:
        return False

    return True


def get_valid_changed_lines(patch: str) -> set[int]:
    """Parse the diff patch to find all line numbers in the new file (RIGHT side) that are part of the diff."""
    valid_lines = set()
    if not patch:
        return valid_lines

    current_line = 0
    for line in patch.splitlines():
        if line.startswith("@@"):
            try:
                # Header format: @@ -old_start,old_count +new_start,new_count @@
                parts = line.split()
                new_info = parts[2].lstrip("+")
                if "," in new_info:
                    start_line, _ = new_info.split(",")
                else:
                    start_line = new_info
                current_line = int(start_line)
            except Exception:
                current_line = 0
        elif line.startswith("+") or line.startswith(" ") or line == "":
            if current_line > 0:
                valid_lines.add(current_line)
                current_line += 1
        elif line.startswith("-"):
            # Deleted lines do not advance line numbers in the new file (RIGHT side)
            pass
    return valid_lines


def filter_review_comments(review: ReviewResult, text_files: list) -> ReviewResult:
    """Filter inline comments to ensure they apply to valid lines in the diff,
    redirecting others to general feedback.
    """
    # Map file path -> set of valid line numbers
    file_patches = {f["filename"]: f.get("patch", "") for f in text_files}
    valid_lines_by_file = {filename: get_valid_changed_lines(patch) for filename, patch in file_patches.items()}

    filtered_comments = []
    redirected_feedback = []

    for comment in review.comments:
        comment_path = comment.path.replace("\\", "/")

        matched_file = None
        for fn in valid_lines_by_file:
            if fn.replace("\\", "/").lower() == comment_path.lower():
                matched_file = fn
                break

        if not matched_file:
            warning_msg = (
                f"Warning: Redirecting inline comment on {comment.path}:{comment.line} (File not found in PR changes)."
            )
            print(warning_msg, file=sys.stderr)

            feedback_item = f"**{comment.path}** (Line {comment.line}): {comment.severity} {comment.comment_text}"
            if comment.code_suggestion:
                feedback_item += f"\n  ```suggestion\n  {comment.code_suggestion}\n  ```"
            redirected_feedback.append(feedback_item)
            continue

        valid_lines = valid_lines_by_file[matched_file]
        if comment.line in valid_lines:
            comment.path = matched_file
            filtered_comments.append(comment)
        else:
            warning_msg = (
                f"Warning: Redirecting inline comment on {comment.path}:{comment.line} (Line not in PR diff patch)."
            )
            print(warning_msg, file=sys.stderr)

            feedback_item = f"**{comment.path}** (Line {comment.line}): {comment.severity} {comment.comment_text}"
            if comment.code_suggestion:
                feedback_item += f"\n  ```suggestion\n  {comment.code_suggestion}\n  ```"
            redirected_feedback.append(feedback_item)

    if redirected_feedback:
        review.general_feedback.append("💡 **Additional Feedback on Unmodified Lines:**")
        review.general_feedback.extend(redirected_feedback)

    review.comments = filtered_comments
    return review


def get_file_content(path: str) -> str:
    """Read file content safely as UTF-8."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def get_pr_files(repository: str, pr_number: int, headers: dict, timeout: int = DEFAULT_TIMEOUT) -> list:
    """Fetch changed files list in PR using pagination."""
    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/files?page={page}&per_page=100"
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            print(f"Error fetching files: {response.status_code} - {response.text}", file=sys.stderr)
            break
        data = response.json()
        if not data:
            break
        files.extend(data)
        page += 1
    return files


def get_local_git_files() -> list:
    """Developer fallback to gather file diffs from local git tree."""
    try:
        res = subprocess.run(["git", "diff", "main...HEAD", "--name-only"], capture_output=True, text=True, check=True)
        filenames = [f.strip() for f in res.stdout.split("\n") if f.strip()]

        files = []
        for filename in filenames:
            diff_res = subprocess.run(
                ["git", "diff", "main...HEAD", "--", filename], capture_output=True, text=True, check=True
            )
            files.append({"filename": filename, "status": "modified", "patch": diff_res.stdout})
        return files

    except Exception as e:
        print(f"Error running local git diff: {e}", file=sys.stderr)
        return []


def load_config() -> dict:
    """Load configuration from gemini-review.toml."""
    path = ".github/commands/gemini-review.toml"
    if not os.path.exists(path):
        action_default_path = os.path.join(os.path.dirname(__file__), "starter-examples", "gemini-review.toml")
        if os.path.exists(action_default_path):
            path = action_default_path
        else:
            return {}

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"Warning: Failed to load config from {path}: {e}", file=sys.stderr)
        return {}


def get_all_repo_files() -> list[str]:
    """Get list of all tracked text files in the repository."""
    try:
        res = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
        all_files = [f.strip() for f in res.stdout.split("\n") if f.strip()]
        return [f.replace("\\", "/") for f in all_files if is_text_file(f) and os.path.exists(f)]
    except Exception as e:
        print(f"Error running git ls-files: {e}", file=sys.stderr)
        # Fallback to os.walk if git is not available
        text_files = []
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                filepath = os.path.relpath(os.path.join(root, file), ".")
                if is_text_file(filepath) and os.path.exists(filepath):
                    text_files.append(filepath.replace("\\", "/"))
        return text_files


def is_core_file(filename: str, patterns: list[str]) -> bool:
    """Check if the filename matches any of the core file patterns."""
    basename = os.path.basename(filename)
    for pattern in patterns:
        if fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(filename, pattern):
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


def load_workspace_rules() -> str:
    """Check for workspace rule files (.agents/AGENTS.md, AGENTS.md, etc.) and return their combined contents."""
    possible_paths = [".agents/AGENTS.md", "AGENTS.md", ".agents/GEMINI.md", "GEMINI.md"]
    rules_content = []
    for path in possible_paths:
        if os.path.exists(path) and os.path.isfile(path):
            try:
                print(f"Loading workspace rules from {path}...", file=sys.stderr)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        rules_content.append(f"=== Rules from {path} ===\n{content}\n")
            except Exception as e:
                print(f"Warning: Failed to load workspace rules from {path}: {e}", file=sys.stderr)

    return "\n".join(rules_content) if rules_content else ""


def parse_skill_metadata(skill_path: str) -> dict[str, str]:
    """Parse name and description from a skill's markdown file frontmatter or first heading."""
    base_name = os.path.basename(skill_path)
    if base_name.lower() in ("skill.md", "readme.md"):
        default_name = os.path.basename(os.path.dirname(skill_path))
    else:
        default_name = os.path.splitext(base_name)[0]
    metadata = {"name": default_name, "description": ""}
    try:
        with open(skill_path, "r", encoding="utf-8-sig") as f:
            content = f.read()
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if match:
            yaml_content = match.group(1)
            yaml_lines = yaml_content.splitlines()
            current_key = None
            for line in yaml_lines:
                if ":" in line and not line.startswith(" "):
                    key, val = line.split(":", 1)
                    key = key.strip().lower()
                    val = val.strip().strip('"').strip("'")
                    if key in ("name", "description"):
                        metadata[key] = val
                        current_key = key
                elif current_key and line.startswith(" "):
                    val = line.strip().strip('"').strip("'")
                    if metadata[current_key] in (">-", ">", "|", "|-"):
                        metadata[current_key] = val
                    else:
                        metadata[current_key] += " " + val
        else:
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            for line in lines:
                if line.startswith("#"):
                    metadata["name"] = line.lstrip("#").strip()
                    break
    except Exception as e:
        print(f"Error parsing skill metadata for {skill_path}: {e}", file=sys.stderr)
    return metadata


def list_available_skills() -> list[dict[str, str]]:
    """Lists all custom agent skills available, including built-in action skills and workspace skills.

    The agent can call load_skill_instructions to retrieve instructions for a specific skill.
    """
    print("Tool Call: list_available_skills() invoked by agent.", file=sys.stderr)
    skills = []

    # 1. Built-in skills packaged with the action
    action_dir = os.path.dirname(__file__)
    built_in_dir = os.path.join(action_dir, "starter-examples", "skills")
    if os.path.isdir(built_in_dir):
        for entry in os.listdir(built_in_dir):
            entry_path = os.path.join(built_in_dir, entry)
            if os.path.isdir(entry_path):
                # Search only for SKILL.md or main .md files at the root of the skill folder
                skill_md = os.path.join(entry_path, "SKILL.md")
                if os.path.isfile(skill_md):
                    meta = parse_skill_metadata(skill_md)
                    meta["id"] = f"builtin:{entry}/SKILL.md"
                    skills.append(meta)
                else:
                    for f in os.listdir(entry_path):
                        if f.endswith(".md") and os.path.isfile(os.path.join(entry_path, f)):
                            meta = parse_skill_metadata(os.path.join(entry_path, f))
                            meta["id"] = f"builtin:{entry}/{f}"
                            skills.append(meta)

    # 2. Workspace-specific skills in the target repo
    skills_dir = ".agents/skills"
    if os.path.isdir(skills_dir):
        for entry in os.listdir(skills_dir):
            entry_path = os.path.join(skills_dir, entry)
            if os.path.isdir(entry_path):
                skill_md = os.path.join(entry_path, "SKILL.md")
                if os.path.isfile(skill_md):
                    meta = parse_skill_metadata(skill_md)
                    meta["id"] = f"{entry}/SKILL.md"
                    skills.append(meta)
                else:
                    for f in os.listdir(entry_path):
                        if f.endswith(".md") and os.path.isfile(os.path.join(entry_path, f)):
                            meta = parse_skill_metadata(os.path.join(entry_path, f))
                            meta["id"] = f"{entry}/{f}"
                            skills.append(meta)
            elif os.path.isfile(entry_path) and entry.endswith(".md"):
                meta = parse_skill_metadata(entry_path)
                meta["id"] = entry
                skills.append(meta)

    return skills


def load_skill_instructions(skill_id: str) -> str:
    """Retrieves the full instructions/rules for a specific skill.

    Args:
        skill_id: The relative path or identifier of the skill (e.g. 'builtin:agent-aware-cli/SKILL.md' or
            'git-workflow-and-versioning.md').
    """
    print(f"Tool Call: load_skill_instructions(skill_id='{skill_id}') invoked by agent.", file=sys.stderr)
    if skill_id.startswith("builtin:"):
        action_dir = os.path.dirname(__file__)
        skills_dir = os.path.realpath(os.path.join(action_dir, "starter-examples", "skills"))
        rel_path = skill_id[len("builtin:") :]
    else:
        skills_dir = os.path.realpath(".agents/skills")
        rel_path = skill_id

    # Normalise separators for safe joining
    rel_path = rel_path.replace("/", os.sep).replace("\\", os.sep)
    safe_path = os.path.realpath(os.path.join(skills_dir, rel_path))

    try:
        if os.path.commonpath([skills_dir, safe_path]) != skills_dir:
            return "Error: Access denied (path traversal blocked)."
    except Exception:
        return "Error: Access denied (path traversal blocked)."

    if os.path.exists(safe_path) and os.path.isfile(safe_path):
        try:
            with open(safe_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            return f"Error reading skill instructions: {e}"

    return f"Error: Skill '{skill_id}' not found."


def get_google_auth_headers() -> dict:
    """Generate authentication headers for calling the Google Developer Knowledge API."""
    headers = {"Content-Type": "application/json"}

    # 1. Use GEMINI_API_KEY if present
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if gemini_api_key:
        headers["X-Goog-Api-Key"] = gemini_api_key
        return headers

    # 2. Try falling back to Google Cloud Application Default Credentials (ADC)
    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        if credentials.token:
            headers["Authorization"] = f"Bearer {credentials.token}"
            return headers
        return {}
    except Exception as e:
        print(f"Warning: Failed to fetch Application Default Credentials for Developer Knowledge: {e}", file=sys.stderr)
        return {}


def search_google_developer_knowledge(query: str) -> str:
    """Searches official Google developer documentation for APIs, best practices, guides, and troubleshooting.

    Args:
        query: The search query, e.g. 'How to configure Google Cloud Run with custom domains'.
    """
    print(f"Tool Call: search_google_developer_knowledge(query='{query}') invoked by agent.", file=sys.stderr)
    headers = get_google_auth_headers()
    if not headers or ("X-Goog-Api-Key" not in headers and "Authorization" not in headers):
        return (
            "Error: No API key or Application Default Credentials found. Google Developer Knowledge Search is"
            " unavailable."
        )

    url = "https://developerknowledge.googleapis.com/mcp"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "search_documents", "arguments": {"query": query}},
        "id": 1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            return f"Error from Google Developer Knowledge API: {response.status_code} - {response.text}"

        data = response.json()
        if "error" in data:
            return f"API Error: {json.dumps(data['error'])}"

        result = data.get("result", {})
        content_list = result.get("content", [])

        text_outputs = []
        for item in content_list:
            if item.get("type") == "text":
                text_outputs.append(item.get("text", ""))

        if not text_outputs:
            return "No matching documentation found."

        return "\n\n".join(text_outputs)
    except Exception as e:
        return f"Error calling Google Developer Knowledge API: {e}"


def get_google_developer_documents(names: list[str]) -> str:
    """Retrieves the full content of one or more documents from the Google developer documentation.

    Args:
        names: A list of document names/URIs returned by search_google_developer_knowledge.
               Format of each name: 'documents/docs.cloud.google.com/...'
    """
    print(f"Tool Call: get_google_developer_documents(names={names}) invoked by agent.", file=sys.stderr)
    headers = get_google_auth_headers()
    if not headers or ("X-Goog-Api-Key" not in headers and "Authorization" not in headers):
        return "Error: No API key or Application Default Credentials found. Document retrieval is unavailable."

    url = "https://developerknowledge.googleapis.com/mcp"
    payload = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_documents", "arguments": {"names": names}},
        "id": 1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            return f"Error from Google Developer Knowledge API: {response.status_code} - {response.text}"

        data = response.json()
        if "error" in data:
            return f"API Error: {json.dumps(data['error'])}"

        result = data.get("result", {})

        content_list = result.get("content", [])

        text_outputs = []
        for item in content_list:
            if item.get("type") == "text":
                text_outputs.append(item.get("text", ""))

        if not text_outputs:
            return "Document content is empty or not found."

        return "\n\n".join(text_outputs)
    except Exception as e:
        return f"Error calling Google Developer Knowledge API: {e}"


def load_system_instruction(repository: str | None, pr_number: int, config: dict) -> str:
    """Load system instructions from Dazbo's gemini-review.toml prompt configuration."""
    prompt = config.get("prompt", "")
    if not prompt:
        return (
            "You are a world-class code review agent. Analyze changes and output constructive feedback using"
            f" {os.environ.get('GEMINI_LANGUAGE', 'English (UK)')} spelling."
        )

    prompt = prompt.replace("!{echo $REPOSITORY}", repository or "unknown")
    prompt = prompt.replace("!{echo $PULL_REQUEST_NUMBER}", str(pr_number))
    prompt = prompt.replace("!{echo $ADDITIONAL_CONTEXT}", "")

    language = os.environ.get("GEMINI_LANGUAGE", "English (UK)")
    prompt = prompt.replace("!{echo $LANGUAGE}", language)
    return prompt


def build_pr_diff_prompt(files: list) -> str:
    """Build the dynamic PR diff patch prompt for modified files."""
    prompt_parts = []
    prompt_parts.append("Below are the files and changes included in this Pull Request:\n")

    for f in files:
        filename = f["filename"]
        status = f["status"]
        patch = f.get("patch", "")

        if not is_text_file(filename) or not patch:
            continue

        full_content = get_file_content(filename)

        prompt_parts.append(f"=== File: {filename} ===")
        prompt_parts.append(f"Status: {status}")
        prompt_parts.append("--- Diff (Patch) ---")
        prompt_parts.append(patch)
        if full_content:
            prompt_parts.append("--- Full Current File Content ---")
            prompt_parts.append(full_content)
        prompt_parts.append("=========================\n")

    return "\n".join(prompt_parts)


def build_codebase_context(files: list, config: dict) -> str:
    """Build the static repository codebase context for caching."""
    prompt_parts = []
    pr_filenames = {f["filename"] for f in files}

    max_context_bytes = config.get("max_context_bytes", 1500 * 1024)
    if "GEMINI_MAX_CONTEXT_BYTES" in os.environ:
        try:
            max_context_bytes = int(os.environ["GEMINI_MAX_CONTEXT_BYTES"])
        except ValueError:
            pass

    core_patterns = config.get(
        "core_file_patterns",
        [
            # Documentation
            "*.md",
            # Python
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "Pipfile",
            # JavaScript / TypeScript / Node
            "package.json",
            "tsconfig.json",
            # Go
            "go.mod",
            # Rust
            "Cargo.toml",
            # Java / Kotlin
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            # Ruby
            "Gemfile",
            "*.gemspec",
            # PHP
            "composer.json",
            # C# / .NET
            "*.csproj",
            "*.sln",
            # Swift / Objective-C
            "Package.swift",
            "Podfile",
            # Docker / Infrastructure
            "Dockerfile",
            "docker-compose.yml",
            # Configuration
            "gemini-review.toml",
            "action.yml",
        ],
    )

    repo_files = get_all_repo_files()
    other_files = [f for f in repo_files if f not in pr_filenames]
    print(
        f"Codebase context: found {len(repo_files)} total tracked files, {len(other_files)} other files"
        " (excluding PR diff files).",
        file=sys.stderr,
    )

    if other_files:
        total_size = 0
        file_sizes = {}
        for f in other_files:
            try:
                size = os.path.getsize(f)
                file_sizes[f] = size
                total_size += size
            except Exception:
                continue

        print(
            f"Codebase context: total size of other text files is {total_size} bytes"
            f" (limit is {max_context_bytes} bytes).",
            file=sys.stderr,
        )

        if total_size <= max_context_bytes:
            print(
                "Codebase context: running in Full Context Mode (attaching all repository text files).", file=sys.stderr
            )
            prompt_parts.append("=== Repository Context (Full Codebase) ===")
            prompt_parts.append("Below are the contents of all other files in this repository for context:\n")
            for f in other_files:
                content = get_file_content(f)
                if content:
                    prompt_parts.append(f"--- File: {f} ---")
                    prompt_parts.append(content)
                    prompt_parts.append("-----------------\n")
            prompt_parts.append("=========================================\n")
        else:
            print(
                "Codebase context: running in Sparse Context Mode (attaching file tree and core"
                " manifests/documentation).",
                file=sys.stderr,
            )
            prompt_parts.append("=== Repository Context (Large Codebase) ===")
            prompt_parts.append(
                "Because this codebase is large, we have included the project file structure and key"
                " configuration/documentation files for context:\n"
            )

            full_tree_files = list(pr_filenames.union(set(other_files)))
            file_tree = generate_file_tree(full_tree_files)
            prompt_parts.append("--- Repository File Structure ---")
            prompt_parts.append(file_tree)
            prompt_parts.append("---------------------------------\n")

            prompt_parts.append("--- Key Configuration and Documentation Files ---")
            core_files_included = []
            for f in other_files:
                if is_core_file(f, core_patterns):
                    content = get_file_content(f)
                    if content:
                        prompt_parts.append(f"--- File: {f} ---")
                        prompt_parts.append(content)
                        prompt_parts.append("-----------------\n")
                        core_files_included.append(f)
            if core_files_included:
                print(
                    f"Codebase context: attached {len(core_files_included)} core configuration/documentation files:"
                    f" {', '.join(core_files_included)}",
                    file=sys.stderr,
                )
            else:
                prompt_parts.append("(No additional key configuration or documentation files found.)\n")
                print("Codebase context: no core files matched or found.", file=sys.stderr)
            prompt_parts.append("==========================================\n")

    return "\n".join(prompt_parts)


def build_prompt(files: list, config: dict) -> str:
    """Consolidate file patches and file contents into a single review context."""
    pr_prompt = build_pr_diff_prompt(files)
    codebase_ctx = build_codebase_context(files, config)
    if codebase_ctx:
        return f"{pr_prompt}\n\n{codebase_ctx}"
    return pr_prompt


def post_review(
    repository: str, pr_number: int, commit_id: str, review: ReviewResult, headers: dict, timeout: int = DEFAULT_TIMEOUT
) -> None:
    """Submit review comments atomically or fall back to individual comments if needed."""
    comments_payload = []
    for c in review.comments:
        body_parts = [f"{c.severity} {c.comment_text}"]
        if c.code_suggestion:
            body_parts.append(f"```suggestion\n{c.code_suggestion}\n```")

        comments_payload.append({"path": c.path, "line": c.line, "side": c.side, "body": "\n\n".join(body_parts)})

    review_body = f"## 📋 Review Summary\n\n{review.summary}\n\n## 🔍 General Feedback\n\n" + "\n".join(
        f"- {f}" for f in review.general_feedback
    )

    payload = {"body": review_body, "event": "COMMENT", "comments": comments_payload}

    url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/reviews"
    print(f"Submitting review to PR #{pr_number} on {repository}...", file=sys.stderr)
    res = requests.post(url, headers=headers, json=payload, timeout=timeout)

    if res.status_code in (200, 201):
        print("Successfully posted PR review atomically.", file=sys.stderr)
        return

    print(f"Warning: Failed to submit review atomically (status {res.status_code}). Error: {res.text}", file=sys.stderr)
    print("Falling back to posting summary and comments individually...", file=sys.stderr)

    # 1. Post review summary as a single comment on the PR conversation
    issue_url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    res_summary = requests.post(issue_url, headers=headers, json={"body": review_body}, timeout=timeout)
    if res_summary.status_code not in (200, 201):
        print(f"Error posting review summary comment: {res_summary.status_code} - {res_summary.text}", file=sys.stderr)

    # 2. Post inline comments one by one
    comments_url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}/comments"
    for idx, c in enumerate(comments_payload):
        c_payload = {"body": c["body"], "commit_id": commit_id, "path": c["path"], "line": c["line"], "side": c["side"]}
        res_comment = requests.post(comments_url, headers=headers, json=c_payload, timeout=timeout)
        if res_comment.status_code in (200, 201):
            print(f"Posted comment {idx + 1}/{len(comments_payload)} successfully.", file=sys.stderr)
        else:
            print(
                f"Error posting comment {idx + 1} on {c['path']} (line {c['line']}): {res_comment.status_code} -"
                f" {res_comment.text}",
                file=sys.stderr,
            )


def main():
    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    repository = os.environ.get("GITHUB_REPOSITORY")
    event_path = os.environ.get("GITHUB_EVENT_PATH")

    use_vertexai = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False").lower() in ("true", "1")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    model_name = os.environ.get("GEMINI_MODEL", os.environ.get("MODEL", "gemini-3.6-flash"))

    try:
        timeout = int(os.environ.get("GEMINI_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_TIMEOUT

    headers = {}
    if github_token:
        print("GitHub API Authentication: using GITHUB_TOKEN.", file=sys.stderr)
        headers["Authorization"] = f"token {github_token}"
    else:
        print("GitHub API Authentication: GITHUB_TOKEN not set.", file=sys.stderr)
    headers["Accept"] = "application/vnd.github.v3+json"

    is_dry_run = False
    pr_number = 1
    head_sha = "mock_head_sha"

    if not event_path or not os.path.exists(event_path):
        print("Warning: GITHUB_EVENT_PATH not set or not found. Running in dry-run/mock mode.", file=sys.stderr)
        is_dry_run = True
    else:
        with open(event_path, encoding="utf-8") as f:
            event_payload = json.load(f)
        event_name = os.environ.get("GITHUB_EVENT_NAME", "")

        if event_name == "pull_request":
            pr_number = event_payload["pull_request"]["number"]
            head_sha = event_payload["pull_request"]["head"]["sha"]
        elif event_name == "issue_comment":
            comment_body = event_payload["comment"]["body"].strip()
            if not comment_body.startswith("/gemini-review"):
                print("Comment does not start with /gemini-review. Exiting gracefully.", file=sys.stderr)
                sys.exit(0)

            author_association = event_payload["comment"]["author_association"]
            allowed_associations = {"OWNER", "MEMBER", "COLLABORATOR"}
            if author_association not in allowed_associations:
                print(
                    f"User association '{author_association}' not authorized to trigger code review. Exiting.",
                    file=sys.stderr,
                )
                sys.exit(0)

            if "pull_request" not in event_payload["issue"]:
                print("Comment is not on a pull request. Exiting.", file=sys.stderr)
                sys.exit(0)

            pr_number = event_payload["issue"]["number"]
            url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"
            res = requests.get(url, headers=headers, timeout=timeout)
            if res.status_code != 200:
                print(f"Error fetching PR details: {res.status_code} - {res.text}", file=sys.stderr)
                sys.exit(1)
            pr_data = res.json()
            head_sha = pr_data["head"]["sha"]
        else:
            print(f"Unsupported event type: {event_name}. Running in dry-run mode.", file=sys.stderr)
            is_dry_run = True

    # Gather file patches and full contents
    if is_dry_run:
        print("Gathering files from local git tree...", file=sys.stderr)
        files = get_local_git_files()
    else:
        print(f"Fetching files for PR #{pr_number} from GitHub API...", file=sys.stderr)
        files = get_pr_files(repository, pr_number, headers, timeout=timeout)

    if not files:
        print("No files modified in this PR. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Filter out excluded file types
    text_files = [f for f in files if is_text_file(f["filename"])]
    if not text_files:
        print("No text-based files to review. Exiting.", file=sys.stderr)
        sys.exit(0)

    # Initialise Gemini client
    if use_vertexai:
        print(f"Initialising GenAI Client (Model: {model_name}) using Vertex AI authentication...", file=sys.stderr)
        client = genai.Client(vertexai=True, project=project, location=location)
    else:
        print(
            f"Initialising GenAI Client (Model: {model_name}) using Google AI Studio API Key authentication...",
            file=sys.stderr,
        )
        client = genai.Client(api_key=gemini_api_key)

    config = load_config()
    system_instruction = load_system_instruction(repository, pr_number, config)

    # Load workspace rules (AGENTS.md, etc.)
    workspace_rules = load_workspace_rules()
    if workspace_rules:
        system_instruction += f"\n\n## Project Rules & Best Practices:\n{workspace_rules}"

    # Assemble tools list
    tools = [list_available_skills, load_skill_instructions]
    auth_headers = get_google_auth_headers()
    disable_dev_k = os.environ.get("DISABLE_DEVELOPER_KNOWLEDGE", "false").lower() == "true"
    has_dev_knowledge = bool(
        not disable_dev_k and auth_headers and ("X-Goog-Api-Key" in auth_headers or "Authorization" in auth_headers)
    )
    if has_dev_knowledge:
        print("Registering Google Developer Knowledge MCP tools...", file=sys.stderr)
        tools.extend([search_google_developer_knowledge, get_google_developer_documents])

    # Add tools info to system instruction
    system_instruction += "\n\n## Tools Availability:"
    system_instruction += (
        "\n- You have workspace skill tools: `list_available_skills` and `load_skill_instructions` to find and load"
        " local project guidelines."
    )
    if has_dev_knowledge:
        system_instruction += (
            "\n- You have Google Developer Knowledge search tools: `search_google_developer_knowledge` and"
            " `get_google_developer_documents` to query official Google APIs, Google Cloud, Firebase, and other"
            " developer docs."
        )

    pr_diff_prompt = build_pr_diff_prompt(text_files)
    codebase_context = build_codebase_context(text_files, config)
    full_prompt = f"{pr_diff_prompt}\n\n{codebase_context}" if codebase_context else pr_diff_prompt

    enable_caching = config.get("enable_context_caching", True)
    cache_ttl_seconds = config.get("cache_ttl_seconds", 3600)
    cache_ttl = f"{cache_ttl_seconds}s"

    cached_content_name = None
    contents_to_send = full_prompt
    is_reused_cache = False

    if enable_caching and hasattr(client, "caches") and codebase_context:
        try:
            # Gemini Context Caching requires minimum 32,768 tokens (approx 100,000+ characters)
            if len(codebase_context) > 100000:
                clean_repo = repository.replace("/", "-").replace("\\", "-") if repository else "repo"
                display_name = f"repo-cache-{clean_repo}"

                # Check if an active cache already exists for this repository display_name
                existing_cache = None
                try:
                    active_caches = client.caches.list()
                    for cache_item in active_caches:
                        if getattr(cache_item, "display_name", None) == display_name:
                            existing_cache = cache_item
                            break
                except Exception as list_err:
                    print(f"Notice: Cache listing failed ({list_err}), creating fresh cache.", file=sys.stderr)

                if existing_cache:
                    cached_content_name = existing_cache.name
                    contents_to_send = pr_diff_prompt
                    is_reused_cache = True
                    print(
                        f"Reusing active Gemini context cache ({display_name}: {cached_content_name})...",
                        file=sys.stderr,
                    )
                else:
                    print(f"Creating Gemini context cache ({display_name})...", file=sys.stderr)
                    parsed_tools = None
                    if tools:
                        try:
                            parsed_cfg = client.models._parse_config(types.GenerateContentConfig(tools=tools))
                            parsed_tools = parsed_cfg.tools
                        except Exception:
                            parsed_tools = None

                    cache_obj = client.caches.create(
                        model=model_name,
                        config=types.CreateCachedContentConfig(
                            contents=[codebase_context],
                            display_name=display_name,
                            system_instruction=system_instruction,
                            tools=parsed_tools,
                            ttl=cache_ttl,
                        ),
                    )
                    cached_content_name = cache_obj.name
                    contents_to_send = pr_diff_prompt
                    is_reused_cache = False
                    print(f"Context cache active: {cached_content_name}", file=sys.stderr)
        except Exception as e:
            print(
                f"Warning: Context caching unavailable or skipped ({e}). Proceeding with direct context.",
                file=sys.stderr,
            )
            cached_content_name = None
            contents_to_send = full_prompt
            is_reused_cache = False

    if cached_content_name:
        gen_config = types.GenerateContentConfig(
            cached_content=cached_content_name,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        )
    else:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            response_mime_type="application/json",
            response_schema=ReviewResult,
        )

    print("Generating code review...", file=sys.stderr)

    response = client.models.generate_content(
        model=model_name,
        contents=contents_to_send,
        config=gen_config,
    )

    if response.usage_metadata:
        usage = response.usage_metadata
        prompt_tokens = usage.prompt_token_count or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0
        candidates_tokens = usage.candidates_token_count or 0
        total_tokens = usage.total_token_count or 0

        fresh_tokens = max(0, prompt_tokens - cached_tokens)
        cache_percentage = (cached_tokens / prompt_tokens * 100) if prompt_tokens > 0 else 0.0

        cache_origin_str = "♻️ Reused (Cross-PR Push)" if is_reused_cache else "✨ Fresh (Newly Created)"
        cache_overhead_str = "⚡ 0s (Reused active handle)" if is_reused_cache else "⚡ 1-Hour TTL Active"

        print("\n📊 Gemini Token Usage & Cost Efficiency Report", file=sys.stderr)
        print(
            "┌──────────────────────────────────────┬──────────────┬───────────────────────────────┐", file=sys.stderr
        )
        print(
            "│ Metric                               │ Token Count  │ Benefit / Efficiency          │", file=sys.stderr
        )
        print(
            "├──────────────────────────────────────┼──────────────┼───────────────────────────────┤", file=sys.stderr
        )
        print(
            f"│ Total Input (Prompt) Tokens          │ {prompt_tokens:>12,d} │ Base input context            │",
            file=sys.stderr,
        )
        if cached_tokens > 0:
            print(
                f"│ ├── Cached Context Tokens            │ {cached_tokens:>12,d} │ ⚡ {cache_percentage:>5.1f}% (75%"
                " Rate Discount)  │",
                file=sys.stderr,
            )
            print(
                f"│ └── Un-cached Fresh Tokens           │ {fresh_tokens:>12,d} │ Diff & instructions only      │",
                file=sys.stderr,
            )
        else:
            print(
                "│ └── Cached Context Tokens            │            0 │ Direct un-cached context      │",
                file=sys.stderr,
            )
        print(
            f"│ Output (Candidates) Tokens           │ {candidates_tokens:>12,d} │ Generated review content      │",
            file=sys.stderr,
        )
        if cached_tokens > 0:
            print(
                "├──────────────────────────────────────┼──────────────┼───────────────────────────────┤",
                file=sys.stderr,
            )
            print(f"│ Cache Lifecycle Origin               │            — │ {cache_origin_str:<29s} │", file=sys.stderr)
            print(
                f"│ Cache Provisioning Overhead          │            — │ {cache_overhead_str:<29s} │", file=sys.stderr
            )
            print(
                "│ Intra-Run Multi-Turn Re-billing      │            — │ 🛡️ 0 Tokens Re-billed / Turn  │",
                file=sys.stderr,
            )
        print(
            "├──────────────────────────────────────┼──────────────┼───────────────────────────────┤", file=sys.stderr
        )
        print(
            f"│ Total Session Tokens                 │ {total_tokens:>12,d} │ Total processed by Gemini     │",
            file=sys.stderr,
        )
        print(
            "└──────────────────────────────────────┴──────────────┴───────────────────────────────┘\n", file=sys.stderr
        )

    review_data = json.loads(response.text)

    review = ReviewResult(**review_data)
    review = filter_review_comments(review, text_files)

    if is_dry_run:
        print("\n=== DRY RUN REVIEW SUMMARY ===", file=sys.stderr)
        print(f"Summary: {review.summary}")
        print("\n=== GENERAL FEEDBACK ===", file=sys.stderr)
        for gf in review.general_feedback:
            print(f"- {gf}")
        print("\n=== INLINE COMMENTS ===", file=sys.stderr)
        for c in review.comments:
            suggestion_str = f"\nSuggestion:\n{c.code_suggestion}" if c.code_suggestion else ""
            print(f"File: {c.path}:{c.line} ({c.side}) - Severity: {c.severity}\n{c.comment_text}{suggestion_str}\n")
    else:
        post_review(repository, pr_number, head_sha, review, headers, timeout=timeout)


if __name__ == "__main__":
    main()
