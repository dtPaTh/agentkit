"""
Artifacts CLI - Cross-platform CLI for managing Claude Code and Qwen Code artifacts

Public API:
    - parse_repo_url: Parse various Git repository URL formats
    - discover_artifacts: Find artifacts in a directory
    - find_artifacts_root: Search for artifact root in a repository
    - parse_skill_md: Parse SKILL.md frontmatter
    - validate_skill_md: Validate SKILL.md format
    - install_skill: Install a skill to a directory
    - pack_skill: Pack a skill into a zip file
    - get_claude_skills_dir: Get Claude skills directory path
    - get_artifacts_dir: Get an artifact directory for a target agent (claude, qwen)
    - convert_artifact: Convert an artifact to a target agent's conventions

CLI Entry Point:
    - main: CLI main function
"""

__version__ = "0.6.0"

from .core import (
    # Constants
    DEFAULT_REPO,
    METADATA_FILE,
    COMMON_SKILL_DIRS,
    REQUIRED_SKILL_FIELDS,
    ARTIFACT_CONFIG,
    DEFAULT_AGENT,
    AGENT_CONFIG,
    get_agent_config,

    # Logging
    Colors,
    log_info,
    log_success,
    log_warning,
    log_error,

    # URL Parsing
    parse_repo_url,

    # Git Operations
    run_git,
    get_git_commit_hash,
    get_remote_head_commit,
    detect_default_branch,
    clone_repo,
    get_repo_cache_dir,

    # Metadata
    write_artifact_metadata,
    read_artifact_metadata,
    write_skill_metadata,
    read_skill_metadata,

    # Skill Discovery
    discover_artifacts,
    find_artifacts_root,
    parse_artifact_md,
    discover_skills,
    find_skills_root,
    parse_skill_md,

    # Installation
    get_artifacts_dir,
    get_claude_artifacts_dir,
    backup_artifact,
    install_artifact,
    pack_artifact,
    get_claude_skills_dir,
    backup_skill,
    install_skill,
    pack_skill,

    # Validation
    validate_artifact_md,
    validate_skill_md,
)

from .convert import Change, convert_artifact, summarize_changes

from .cli import main

__all__ = [
    # Constants
    "DEFAULT_REPO",
    "METADATA_FILE",
    "COMMON_SKILL_DIRS",
    "REQUIRED_SKILL_FIELDS",
    "ARTIFACT_CONFIG",
    "DEFAULT_AGENT",
    "AGENT_CONFIG",
    "get_agent_config",

    # Logging
    "Colors",
    "log_info",
    "log_success",
    "log_warning",
    "log_error",

    # URL Parsing
    "parse_repo_url",

    # Git Operations
    "run_git",
    "get_git_commit_hash",
    "get_remote_head_commit",
    "detect_default_branch",
    "clone_repo",
    "get_repo_cache_dir",

    # Metadata
    "write_artifact_metadata",
    "read_artifact_metadata",
    "write_skill_metadata",
    "read_skill_metadata",

    # Skill Discovery
    "discover_artifacts",
    "find_artifacts_root",
    "parse_artifact_md",
    "discover_skills",
    "find_skills_root",
    "parse_skill_md",

    # Installation
    "get_artifacts_dir",
    "get_claude_artifacts_dir",
    "backup_artifact",
    "install_artifact",
    "pack_artifact",
    "get_claude_skills_dir",
    "backup_skill",
    "install_skill",
    "pack_skill",

    # Validation
    "validate_artifact_md",
    "validate_skill_md",

    # Conversion
    "Change",
    "convert_artifact",
    "summarize_changes",

    # CLI
    "main",
]
