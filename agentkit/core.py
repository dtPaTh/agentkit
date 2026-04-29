"""
AgentKit Core Library

This module contains all core logic independent of the CLI interface,
allowing reuse by other Python programs.

Main Features:
    - Git operations: clone, sparse checkout, branch detection
    - Skill discovery: find and parse skills in directories
    - Installation management: install, backup, metadata tracking
    - Packaging: pack skills for Claude Desktop
    - Validation: validate SKILL.md format

Design Principles:
    - Zero dependencies: only uses Python standard library
    - Pure functions: prefer pure functions for easier testing
    - Clear error handling: return explicit error messages on failure
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# =============================================================================
# Global Configuration
# =============================================================================

# Default to Anthropic's official skills repo
DEFAULT_REPO = os.environ.get("AGENTKIT_DEFAULT_REPO", "https://github.com/anthropics/skills/tree/main/skills")

# Metadata file name for tracking installation source
METADATA_FILE = ".agentkit.json"

# Required fields in SKILL.md
REQUIRED_SKILL_FIELDS = ["name", "description"]

# Common skill subdirectory locations
COMMON_SKILL_DIRS = [
    "skills",
    "claude-skills",
    ".claude/skills",
    "claude/skills",
    "src/skills",
]


ARTIFACT_CONFIG = {
    "skills": {
        "label": "skill",
        "label_plural": "skills",
        "marker": "SKILL.md",
        "required_fields": REQUIRED_SKILL_FIELDS,
        "common_dirs": COMMON_SKILL_DIRS,
    },
    "commands": {
        "label": "command",
        "label_plural": "commands",
        "marker": "COMMAND.md",
        "required_fields": ["name", "description"],
        "common_dirs": [
            "commands",
            ".claude/commands",
            "claude/commands",
            "src/commands",
        ],
    },
    "agents": {
        "label": "agent",
        "label_plural": "agents",
        "marker": "AGENT.md",
        "flat_file_suffix": ".agent.md",
        "required_fields": ["name", "description"],
        "common_dirs": [
            "agents",
            ".claude/agents",
            "claude/agents",
            "src/agents",
        ],
    },
    "plugins": {
        "label": "plugin",
        "label_plural": "plugins",
        "marker": "PLUGIN.md",
        "alt_markers": ["plugin.json"],
        "required_fields": ["name", "description"],
        "common_dirs": [
            "plugins",
            ".claude/plugins",
            "claude/plugins",
            "src/plugins",
        ],
    },
}


def get_artifact_config(artifact_type: str = "skills") -> dict:
    """Return normalized artifact configuration."""
    normalized = (artifact_type or "skills").strip().lower()
    if normalized not in ARTIFACT_CONFIG:
        raise ValueError(f"Unsupported artifact type: {artifact_type}")
    return ARTIFACT_CONFIG[normalized]


# =============================================================================
# Terminal Color Handling
# =============================================================================

class Colors:
    """
    ANSI color code wrapper class.

    Design considerations:
        - Uses class attributes instead of instance, as colors are global settings
        - Provides disable() method for older Windows cmd compatibility
    """
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"

    @classmethod
    def disable(cls):
        """Disable all color output."""
        cls.RESET = cls.BOLD = cls.RED = cls.GREEN = ""
        cls.YELLOW = cls.BLUE = cls.CYAN = ""


# Windows terminal compatibility handling
if sys.platform == "win32" and not os.environ.get("WT_SESSION"):
    try:
        import colorama
        colorama.init()
    except ImportError:
        Colors.disable()


# =============================================================================
# Logging Functions
# =============================================================================

def log_info(msg: str):
    """Info message (blue ℹ)."""
    print(f"{Colors.BLUE}ℹ{Colors.RESET} {msg}")


def log_success(msg: str):
    """Success message (green ✓)."""
    print(f"{Colors.GREEN}✓{Colors.RESET} {msg}")


def log_warning(msg: str):
    """Warning message (yellow ⚠)."""
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {msg}")


def log_error(msg: str):
    """Error message (red ✗)."""
    print(f"{Colors.RED}✗{Colors.RESET} {msg}", file=sys.stderr)


# =============================================================================
# URL Parsing
# =============================================================================

def parse_repo_url(url: str) -> dict:
    """
    Parse Git repo URL and extract its components.

    Supported URL formats:
        - GitHub browser URL: https://github.com/owner/repo/tree/branch/subdir
        - GitLab browser URL: https://gitlab.com/owner/repo/-/tree/branch/subdir
        - Plain HTTPS URL: https://github.com/owner/repo
        - SSH URL: git@github.com:owner/repo.git

    Returns:
        dict with keys: url, clone_url, branch, subdir, host
    """
    result = {
        "url": url,
        "clone_url": None,
        "branch": "main",
        "subdir": None,
        "host": None,
    }

    # GitHub tree URL
    github_tree_match = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.+))?",
        url
    )
    if github_tree_match:
        owner, repo, branch, subdir = github_tree_match.groups()
        result["clone_url"] = f"https://github.com/{owner}/{repo}.git"
        result["branch"] = branch
        result["subdir"] = subdir
        result["host"] = "github"
        return result

    # GitLab tree URL
    gitlab_tree_match = re.match(
        r"(https://[^/]+)/([^/]+/[^/]+)/-/tree/([^/]+)(?:/(.+))?",
        url
    )
    if gitlab_tree_match:
        host, repo_path, branch, subdir = gitlab_tree_match.groups()
        result["clone_url"] = f"{host}/{repo_path}.git"
        result["branch"] = branch
        result["subdir"] = subdir
        result["host"] = "gitlab"
        return result

    # Plain HTTPS URL
    if url.startswith("https://") or url.startswith("http://"):
        parsed = urlparse(url)
        result["host"] = parsed.netloc
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            result["clone_url"] = url
        else:
            result["clone_url"] = f"{parsed.scheme}://{parsed.netloc}{path}.git"
        return result

    # SSH URL
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", url)
    if ssh_match:
        host, repo_path = ssh_match.groups()
        result["clone_url"] = f"git@{host}:{repo_path}.git"
        result["host"] = host
        return result

    result["clone_url"] = url
    return result


# =============================================================================
# Git Operations
# =============================================================================

def run_git(args: list, cwd: Optional[Path] = None, capture: bool = True) -> subprocess.CompletedProcess:
    """
    Unified interface for executing Git commands.

    Args:
        args: Git command arguments (without 'git' itself)
        cwd: Working directory
        capture: Whether to capture output
    """
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            check=True
        )
        return result
    except subprocess.CalledProcessError as e:
        log_error(f"Git command failed: {' '.join(cmd)}")
        if e.stderr:
            log_error(e.stderr.strip())
        raise


def get_git_commit_hash(repo_dir: Path) -> Optional[str]:
    """Get the current commit hash (short version) of a Git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def detect_default_branch(clone_url: str) -> str:
    """
    Auto-detect the default branch name of a remote repo.

    Uses `git ls-remote --symref` to query, defaults to "main" on failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", clone_url, "HEAD"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if line.startswith("ref: refs/heads/"):
                    branch = line.split("refs/heads/")[1].split()[0]
                    return branch
    except (subprocess.TimeoutExpired, Exception):
        pass
    return "main"


def get_repo_cache_dir(repo_info: dict) -> Path:
    """Return a stable per-repo cache directory under ~/.cache/agentkit/."""
    key = hashlib.md5(f"{repo_info['clone_url']}#{repo_info['branch']}".encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "agentkit" / key


def clone_repo(repo_info: dict, target_dir: Path, force_refresh: bool = False) -> Path:
    """
    Clone a Git repo to target_dir, or fetch+reset if already cached.

    Supports sparse checkout to only download required subdirectories.

    Returns:
        The actual artifact root directory path
    """
    clone_url = repo_info["clone_url"]
    branch = repo_info["branch"]
    subdir = repo_info["subdir"]
    already_cached = (target_dir / ".git").exists()

    if already_cached and not force_refresh:
        log_info(f"Updating cached repo {clone_url} (branch: {branch})")
        run_git(["fetch", "--depth=1", "origin", branch], cwd=target_dir)
        run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target_dir)
        return target_dir / subdir if subdir else target_dir

    if already_cached and force_refresh:
        def _force_remove(func, path, _):
            os.chmod(path, 0o666)
            func(path)
        shutil.rmtree(target_dir, onerror=_force_remove)
        target_dir.mkdir(parents=True, exist_ok=True)

    log_info(f"Cloning from {clone_url} (branch: {branch})")

    if subdir:
        log_info(f"Using sparse checkout for subdirectory: {subdir}")

        target_dir.mkdir(parents=True, exist_ok=True)
        run_git(["init"], cwd=target_dir)
        run_git(["remote", "add", "origin", clone_url], cwd=target_dir)

        run_git(["config", "core.sparseCheckout", "true"], cwd=target_dir)
        sparse_file = target_dir / ".git" / "info" / "sparse-checkout"
        sparse_file.parent.mkdir(parents=True, exist_ok=True)
        sparse_file.write_text(f"{subdir}/*\n")

        run_git(["pull", "origin", branch, "--depth=1"], cwd=target_dir)

        return target_dir / subdir
    else:
        run_git([
            "clone",
            "--depth=1",
            "--branch", branch,
            clone_url,
            str(target_dir)
        ])
        return target_dir


# =============================================================================
# Metadata Tracking
# =============================================================================

def write_artifact_metadata(artifact_dir: Path, repo_info: dict, commit_hash: Optional[str] = None):
    """Write installation source metadata file in the artifact directory."""
    metadata = {
        "source_url": repo_info.get("url"),
        "clone_url": repo_info.get("clone_url"),
        "branch": repo_info.get("branch"),
        "commit": commit_hash,
        "installed_at": datetime.now().isoformat(),
        "installed_by": "agentkit",
    }
    metadata_path = artifact_dir / METADATA_FILE
    metadata_path.write_text(json.dumps(metadata, indent=2))


def read_artifact_metadata(artifact_dir: Path) -> Optional[dict]:
    """Read the metadata file from an artifact directory."""
    metadata_path = artifact_dir / METADATA_FILE
    if metadata_path.exists():
        try:
            return json.loads(metadata_path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return None


# =============================================================================
# Skill Discovery and Parsing
# =============================================================================

def discover_artifacts(artifacts_dir: Path, artifact_type: str = "skills") -> list[dict]:
    """
    Discover all artifacts of a type in a directory.

    Supports three layouts:
      - Subdirectory with primary marker (e.g. SKILL.md)
      - Subdirectory with alternative marker (e.g. plugin.json)
      - Flat files matching a suffix (e.g. .agent.md)

    Returns:
        Each artifact is a dict containing path, folder_name, name, description
    """
    config = get_artifact_config(artifact_type)
    marker_file = config["marker"]
    alt_markers = config.get("alt_markers", [])
    flat_file_suffix = config.get("flat_file_suffix")
    artifacts = []

    if not artifacts_dir.exists():
        return artifacts

    for item in artifacts_dir.iterdir():
        if item.is_dir():
            marker_path = item / marker_file
            if not marker_path.exists():
                for alt in alt_markers:
                    candidate = item / alt
                    if candidate.exists():
                        marker_path = candidate
                        break
                else:
                    continue
            artifact_info = parse_artifact_md(marker_path, artifact_type=artifact_type)
            artifact_info["path"] = item
            artifact_info["folder_name"] = item.name
            artifacts.append(artifact_info)
        elif flat_file_suffix and item.is_file() and item.name.endswith(flat_file_suffix):
            artifact_info = parse_artifact_md(item, artifact_type=artifact_type)
            stem = item.name[: -len(flat_file_suffix)]
            if not artifact_info.get("name"):
                artifact_info["name"] = stem
            artifact_info["path"] = item
            artifact_info["folder_name"] = stem
            artifacts.append(artifact_info)

    return sorted(artifacts, key=lambda x: x.get("name", x["folder_name"]))


def find_artifacts_root(repo_root: Path, artifact_type: str = "skills", quiet: bool = False) -> tuple[Path, list[dict]]:
    """
    Find the artifact root directory in a repo.

    Search order:
        1. Root directory itself
        2. Common subdirectory names
        3. Recursive search for directories containing marker files

    Returns:
        (artifacts_root, artifacts_list)
    """
    config = get_artifact_config(artifact_type)
    marker_file = config["marker"]
    common_dirs = config["common_dirs"]

    def _info(msg: str) -> None:
        if not quiet:
            log_info(msg)

    # 1. Try root directory first
    artifacts = discover_artifacts(repo_root, artifact_type=artifact_type)
    if artifacts:
        return repo_root, artifacts

    # 2. Try common subdirectories
    for subdir in common_dirs:
        candidate = repo_root / subdir
        if candidate.exists() and candidate.is_dir():
            artifacts = discover_artifacts(candidate, artifact_type=artifact_type)
            if artifacts:
                _info(f"Found {config['label_plural']} in: {subdir}/")
                return candidate, artifacts

    # 3. Search for marker files (up to 3 levels deep)
    marker_files = []
    for depth in range(1, 4):
        pattern = "/".join(["*"] * depth) + f"/{marker_file}"
        marker_files.extend(repo_root.glob(pattern))
        if marker_files:
            break

    if marker_files:
        parents = set()
        for marker_path in marker_files:
            artifact_folder = marker_path.parent
            artifacts_root = artifact_folder.parent
            parents.add(artifacts_root)

        if len(parents) == 1:
            artifacts_root = parents.pop()
            relative = artifacts_root.relative_to(repo_root)
            if str(relative) != ".":
                _info(f"Found {config['label_plural']} in: {relative}/")
            artifacts = discover_artifacts(artifacts_root, artifact_type=artifact_type)
            if artifacts:
                return artifacts_root, artifacts
        else:
            _info(f"Found {config['label_plural']} in multiple directories")
            all_artifacts = []
            for marker_path in marker_files:
                artifact_folder = marker_path.parent
                artifact_info = parse_artifact_md(marker_path, artifact_type=artifact_type)
                artifact_info["path"] = artifact_folder
                artifact_info["folder_name"] = artifact_folder.name
                all_artifacts.append(artifact_info)
            if all_artifacts:
                return repo_root, sorted(all_artifacts, key=lambda x: x.get("name", x["folder_name"]))

    return repo_root, []


def parse_artifact_md(marker_md: Path, artifact_type: str = "skills") -> dict:
    """
    Parse an artifact marker file (YAML frontmatter or JSON).

    Returns:
        dict with name and description (may be None)
    """
    content = marker_md.read_text(encoding="utf-8")

    result = {
        "name": None,
        "description": None,
    }

    if marker_md.suffix == ".json":
        try:
            data = json.loads(content)
            result["name"] = data.get("name")
            desc = data.get("description")
            result["description"] = str(desc).strip() if desc else None
        except (json.JSONDecodeError, ValueError):
            pass
        return result

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1].strip()
            for line in frontmatter.split("\n"):
                if ":" in line:
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip().strip('"').strip("'")
                    if key in ("name", "description"):
                        result[key] = value

    return result


def discover_skills(skills_dir: Path) -> list[dict]:
    """Backward compatible wrapper for skills discovery."""
    return discover_artifacts(skills_dir, artifact_type="skills")


def find_skills_root(repo_root: Path) -> tuple[Path, list[dict]]:
    """Backward compatible wrapper for skills root discovery."""
    return find_artifacts_root(repo_root, artifact_type="skills")


def parse_skill_md(skill_md: Path) -> dict:
    """Backward compatible wrapper for SKILL.md parsing."""
    return parse_artifact_md(skill_md, artifact_type="skills")


# =============================================================================
# Installation and Management
# =============================================================================

def get_claude_artifacts_dir(artifact_type: str = "skills", scope: str = "personal") -> Path:
    """
    Get the Claude Code artifact installation directory.

    Args:
        scope: "personal" (global) or "project"
    """
    get_artifact_config(artifact_type)
    if scope == "project":
        return Path.cwd() / ".claude" / artifact_type
    else:
        return Path.home() / ".claude" / artifact_type


def backup_artifact(artifact_path: Path) -> Optional[Path]:
    """
    Backup an existing artifact directory.

    Returns:
        Backup path, or None if directory doesn't exist
    """
    if not artifact_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = artifact_path.parent / ".backup"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"{artifact_path.name}_{timestamp}"

    if artifact_path.is_file():
        shutil.copy2(artifact_path, backup_path)
    else:
        shutil.copytree(artifact_path, backup_path)
    return backup_path


def install_artifact(
    artifact_path: Path,
    target_dir: Path,
    artifact_type: str = "skills",
    repo_info: Optional[dict] = None,
    commit_hash: Optional[str] = None,
    force: bool = False,
    backup: bool = False,
    dry_run: bool = False
) -> tuple[bool, str]:
    """
    Install a single artifact to the target directory.

    Returns:
        (success, message) tuple
    """
    config = get_artifact_config(artifact_type)
    label = config["label"]
    artifact_name = artifact_path.name
    dest_path = target_dir / artifact_name
    action = "install"

    is_file = artifact_path.is_file()

    if dest_path.exists():
        if not force:
            return (False, f"already exists (use --force to overwrite)")
        action = "overwrite"

        if dry_run:
            return (True, f"would overwrite existing {label}")

        if backup:
            backup_path = backup_artifact(dest_path)
            if backup_path:
                log_info(f"Backed up to: {backup_path}")

        if dest_path.is_file():
            dest_path.unlink()
        else:
            shutil.rmtree(dest_path)
    else:
        if dry_run:
            return (True, f"would install to {dest_path}")

    if is_file:
        shutil.copy2(artifact_path, dest_path)
    else:
        shutil.copytree(artifact_path, dest_path)
        if repo_info:
            write_artifact_metadata(dest_path, repo_info, commit_hash)

    return (True, "installed" if action == "install" else "updated")


def pack_artifact(artifact_path: Path, output_dir: Path) -> Path:
    """
    Pack an artifact into a zip file.

    Returns:
        Path to the zip file
    """
    artifact_name = artifact_path.name
    zip_path = output_dir / f"{artifact_name}.zip"

    output_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in artifact_path.rglob("*"):
            if file_path.is_file():
                arcname = file_path.relative_to(artifact_path.parent)
                zf.write(file_path, arcname)

    log_success(f"Packed: {zip_path}")
    return zip_path


def get_claude_skills_dir(scope: str = "personal") -> Path:
    """Backward compatible wrapper for skills target directory."""
    return get_claude_artifacts_dir(artifact_type="skills", scope=scope)


def backup_skill(skill_path: Path) -> Optional[Path]:
    """Backward compatible wrapper for skill backup."""
    return backup_artifact(skill_path)


def install_skill(
    skill_path: Path,
    target_dir: Path,
    repo_info: Optional[dict] = None,
    commit_hash: Optional[str] = None,
    force: bool = False,
    backup: bool = False,
    dry_run: bool = False
) -> tuple[bool, str]:
    """Backward compatible wrapper for skill install."""
    return install_artifact(
        skill_path,
        target_dir,
        artifact_type="skills",
        repo_info=repo_info,
        commit_hash=commit_hash,
        force=force,
        backup=backup,
        dry_run=dry_run,
    )


def pack_skill(skill_path: Path, output_dir: Path) -> Path:
    """Backward compatible wrapper for skill packing."""
    return pack_artifact(skill_path, output_dir)


# =============================================================================
# Validation
# =============================================================================

def validate_artifact_md(artifact_path: Path, artifact_type: str = "skills") -> list[str]:
    """
    Validate an artifact marker file format.

    Returns:
        List of issues, empty list if validation passes
    """
    config = get_artifact_config(artifact_type)
    marker_file = config["marker"]
    required_fields = config["required_fields"]
    label = config["label"]

    issues = []
    marker_md = artifact_path / marker_file

    if not marker_md.exists():
        issues.append(f"Missing {marker_file} file")
        return issues

    try:
        content = marker_md.read_text(encoding="utf-8")
    except Exception as e:
        issues.append(f"Cannot read {marker_file}: {e}")
        return issues

    if not content.startswith("---"):
        issues.append("Missing YAML frontmatter (should start with ---)")
        return issues

    parts = content.split("---", 2)
    if len(parts) < 3:
        issues.append("Invalid YAML frontmatter (missing closing ---)")
        return issues

    frontmatter = parts[1].strip()
    body = parts[2].strip()

    metadata = {}
    for line in frontmatter.split("\n"):
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip().lower()] = value.strip().strip('"').strip("'")

    for field in required_fields:
        if field not in metadata or not metadata[field]:
            issues.append(f"Missing required field: {field}")

    if not body:
        issues.append(f"Empty {label} body (no instructions after frontmatter)")

    if metadata.get("name") and len(metadata["name"]) > 50:
        issues.append("Name is too long (>50 characters)")

    if metadata.get("description") and len(metadata["description"]) > 500:
        issues.append("Description is too long (>500 characters)")

    return issues


def validate_skill_md(skill_path: Path) -> list[str]:
    """Backward compatible wrapper for skill validation."""
    return validate_artifact_md(skill_path, artifact_type="skills")


def write_skill_metadata(skill_dir: Path, repo_info: dict, commit_hash: Optional[str] = None):
    """Backward compatible wrapper for skill metadata writes."""
    write_artifact_metadata(skill_dir, repo_info, commit_hash)


def read_skill_metadata(skill_dir: Path) -> Optional[dict]:
    """Backward compatible wrapper for skill metadata reads."""
    return read_artifact_metadata(skill_dir)
