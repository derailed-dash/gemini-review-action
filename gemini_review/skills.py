"""
Description: Skills metadata parser and instruction loader module.
Discovers custom agent skills in workspace and action directories,
parses skill frontmatter metadata, and safely retrieves instructions.
"""

import os
import re
import sys

from gemini_review.utils import _get_pr_review_func


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
    fn_parse_skill_metadata = _get_pr_review_func("parse_skill_metadata", parse_skill_metadata)
    print("Tool Call: list_available_skills() invoked by agent.", file=sys.stderr)
    skills = []

    # 1. Built-in skills packaged with the action
    action_dir = os.path.dirname(os.path.dirname(__file__))
    built_in_dir = os.path.join(action_dir, "starter-examples", "skills")
    if os.path.isdir(built_in_dir):
        for entry in os.listdir(built_in_dir):
            entry_path = os.path.join(built_in_dir, entry)
            if os.path.isdir(entry_path):
                # Search only for SKILL.md or main .md files at the root of the skill folder
                skill_md = os.path.join(entry_path, "SKILL.md")
                if os.path.isfile(skill_md):
                    meta = fn_parse_skill_metadata(skill_md)
                    meta["id"] = f"builtin:{entry}/SKILL.md"
                    skills.append(meta)
                else:
                    for f in os.listdir(entry_path):
                        if f.endswith(".md") and os.path.isfile(os.path.join(entry_path, f)):
                            meta = fn_parse_skill_metadata(os.path.join(entry_path, f))
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
                    meta = fn_parse_skill_metadata(skill_md)
                    meta["id"] = f"{entry}/SKILL.md"
                    skills.append(meta)
                else:
                    for f in os.listdir(entry_path):
                        if f.endswith(".md") and os.path.isfile(os.path.join(entry_path, f)):
                            meta = fn_parse_skill_metadata(os.path.join(entry_path, f))
                            meta["id"] = f"{entry}/{f}"
                            skills.append(meta)
            elif os.path.isfile(entry_path) and entry.endswith(".md"):
                meta = fn_parse_skill_metadata(entry_path)
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
        action_dir = os.path.dirname(os.path.dirname(__file__))
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
