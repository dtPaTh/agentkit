"""Artifacts CLI Command Line Interface

This module handles CLI argument parsing and command processing.
Core logic is provided by the core module.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_REPO,
    ARTIFACT_CONFIG,
    Colors,
    log_info,
    log_success,
    log_warning,
    log_error,
    parse_repo_url,
    run_git,
    get_git_commit_hash,
    detect_default_branch,
    clone_repo,
    get_repo_cache_dir,
    read_artifact_metadata,
    get_remote_head_commit,
    discover_artifacts,
    find_artifacts_root,
    get_claude_artifacts_dir,
    install_artifact,
    pack_artifact,
    validate_artifact_md,
)


# =============================================================================
# Helper Functions
# =============================================================================

def get_repo_from_args(args) -> str:
    """Get repository URL from args, supporting both positional and --repo flag."""
    # Positional argument takes precedence if provided
    if hasattr(args, 'repo_url') and args.repo_url:
        return args.repo_url
    return args.repo


def prepare_repo_info(args) -> dict:
    """Prepare repo info, handling branch override and auto-detection."""
    repo_url = get_repo_from_args(args)
    repo_info = parse_repo_url(repo_url)

    if hasattr(args, 'branch') and args.branch:
        repo_info["branch"] = args.branch
    elif repo_info["branch"] == "main" and "/tree/" not in repo_url:
        log_info("Auto-detecting default branch...")
        repo_info["branch"] = detect_default_branch(repo_info["clone_url"])

    return repo_info


def get_artifact_type(args) -> str:
    """Get normalized artifact type from CLI args."""
    artifact_type = getattr(args, "type", "skills") or "skills"
    return artifact_type.strip().lower()


def get_cloned_repo(repo_info: dict, args) -> Path:
    """Return the cloned repo root, using a persistent cache unless --no-cache is set."""
    force_refresh = getattr(args, "no_cache", False)
    cache_dir = get_repo_cache_dir(repo_info)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return clone_repo(repo_info, cache_dir, force_refresh=force_refresh)


def resolve_k8s_positionals(args) -> None:
    """Interpret k8s-style positional args [type] [url] and apply to args in-place.

    Accepts any order: type token is matched against known artifact types,
    everything else is treated as a URL.
    """
    known_types = set(ARTIFACT_CONFIG.keys()) | {"all"}
    positionals = getattr(args, "positionals", None) or []
    for token in positionals:
        if token.lower() in known_types:
            args.type = token.lower()
        else:
            args.repo_url = token


def get_scope(args) -> str:
    """Resolve installation scope: 'project' or 'personal'.

    --scope local  (or legacy --project) → project
    --scope global (default)             → personal
    """
    scope_flag = getattr(args, "scope", "global") or "global"
    project_flag = getattr(args, "project", False)
    return "project" if (scope_flag == "local" or project_flag) else "personal"


def get_artifact_labels(artifact_type: str) -> tuple[str, str]:
    """Get singular/plural labels for an artifact type."""
    config = ARTIFACT_CONFIG[artifact_type]
    return config["label"], config["label_plural"]


def format_artifacts_list(
    artifacts: list[dict],
    detailed: bool = False,
    show_source: bool = False,
) -> None:
    """Format and output artifact list."""
    if detailed:
        name_width = max(len(s.get("name") or s["folder_name"]) for s in artifacts)
        name_width = max(name_width, 4)

        print(f"\n  {'Name':<{name_width}}  Description")
        print(f"  {'-' * name_width}  {'-' * 50}")

        for artifact in artifacts:
            name = artifact.get("name") or artifact["folder_name"]
            desc = artifact.get("description") or "-"
            if len(desc) > 60:
                desc = desc[:57] + "..."
            print(f"  {Colors.CYAN}{name:<{name_width}}{Colors.RESET}  {desc}")

            if show_source:
                metadata = read_artifact_metadata(artifact.get("path"))
                if metadata:
                    source = metadata.get("source_url", "-")
                    branch = metadata.get("branch", "-")
                    commit = metadata.get("commit", "-")
                    version = artifact.get("version") or "n/a"
                    if len(source) > 50:
                        source = source[:47] + "..."
                    print(f"  {' ' * name_width}  {Colors.YELLOW}Version: {version} ({branch})  ↳ {source}{Colors.RESET}")

                    is_outdated = artifact.get("_is_outdated")
                    remote_commit = artifact.get("_remote_commit")
                    if is_outdated is True:
                        print(f"  {' ' * name_width}  {Colors.RED}⬆ Update available  (installed: {commit}  upstream: {remote_commit}){Colors.RESET}")
                    elif is_outdated is False:
                        print(f"  {' ' * name_width}  {Colors.GREEN}✓ Up to date{Colors.RESET}")
                    elif is_outdated is None and "_is_outdated" in artifact:
                        print(f"  {' ' * name_width}  {Colors.YELLOW}? Could not reach upstream{Colors.RESET}")
    else:
        print()
        for artifact in artifacts:
            name = artifact.get("name") or artifact["folder_name"]
            print(f"  {Colors.CYAN}-{Colors.RESET} {name}")


def interactive_select(artifacts: list[dict], artifact_type: str) -> list[dict]:
    """Interactive artifact selection interface."""
    _, label_plural = get_artifact_labels(artifact_type)
    print(f"\n{Colors.BOLD}Available {label_plural.title()}:{Colors.RESET}\n")

    for i, artifact in enumerate(artifacts, 1):
        name = artifact.get("name") or artifact["folder_name"]
        desc = artifact.get("description", "No description")
        print(f"  {Colors.CYAN}{i:3}{Colors.RESET}. {Colors.BOLD}{name}{Colors.RESET}")
        print(f"       {Colors.YELLOW}{desc}{Colors.RESET}")

    print(f"\n{Colors.BOLD}Enter selection:{Colors.RESET}")
    print("  - 'all' or '*' to install all")
    print("  - Comma-separated numbers (e.g., 1,3,5)")
    print("  - Range (e.g., 1-5)")
    print("  - 'q' to quit\n")

    try:
        selection = input(f"{Colors.GREEN}>{Colors.RESET} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return []

    if selection in ("q", "quit", "exit"):
        return []

    if selection in ("all", "*", ""):
        return artifacts

    selected = []
    try:
        for part in selection.split(","):
            part = part.strip()
            if "-" in part:
                start, end = map(int, part.split("-"))
                for i in range(start, end + 1):
                    if 1 <= i <= len(artifacts):
                        selected.append(artifacts[i - 1])
            else:
                i = int(part)
                if 1 <= i <= len(artifacts):
                    selected.append(artifacts[i - 1])
    except ValueError:
        log_error("Invalid selection format")
        return []

    return selected


# =============================================================================
# CLI Commands
# =============================================================================

def cmd_list(args):
    """list command: List available artifacts from a remote repo."""
    resolve_k8s_positionals(args)
    artifact_type = get_artifact_type(args)
    repo_info = prepare_repo_info(args)

    cloned_root = get_cloned_repo(repo_info, args)
    repo_url = get_repo_from_args(args)

    if artifact_type == "all":
        types_to_scan = list(ARTIFACT_CONFIG.keys())
        total = 0
        found_any = False
        print(f"\n{Colors.BOLD}Artifacts in {repo_url}:{Colors.RESET}")
        for atype in types_to_scan:
            _, artifacts = find_artifacts_root(cloned_root, artifact_type=atype, quiet=True)
            if artifacts:
                found_any = True
                _, label_plural = get_artifact_labels(atype)
                print(f"\n{Colors.BOLD}{label_plural.title()} ({len(artifacts)}):{Colors.RESET}")
                format_artifacts_list(artifacts, detailed=args.detail)
                total += len(artifacts)
        if not found_any:
            log_warning("No artifacts found in repository")
            return 1
        print(f"\n{Colors.GREEN}Total: {total} artifacts{Colors.RESET}\n")
    else:
        _, label_plural = get_artifact_labels(artifact_type)
        _, artifacts = find_artifacts_root(cloned_root, artifact_type=artifact_type)
        if not artifacts:
            log_warning(f"No {label_plural} found in repository")
            return 1
        print(f"\n{Colors.BOLD}{label_plural.title()} in {repo_url}:{Colors.RESET}")
        format_artifacts_list(artifacts, detailed=args.detail)
        print(f"\n{Colors.GREEN}Total: {len(artifacts)} {label_plural}{Colors.RESET}\n")

    return 0


def enrich_artifacts_with_update_status(artifacts: list[dict]) -> None:
    """Fetch remote HEAD for each artifact and annotate with update status in-place."""
    for artifact in artifacts:
        meta = read_artifact_metadata(artifact.get("path"))
        if not meta or not meta.get("clone_url") or not meta.get("commit"):
            continue
        log_info(f"Checking {artifact.get('name') or artifact['folder_name']}...")
        remote_sha = get_remote_head_commit(meta["clone_url"], meta.get("branch", "main"))
        if remote_sha:
            artifact["_remote_commit"] = remote_sha[:7]
            artifact["_is_outdated"] = not remote_sha.startswith(meta["commit"])
        else:
            artifact["_remote_commit"] = None
            artifact["_is_outdated"] = None  # could not reach upstream


def cmd_installed(args):
    """installed command: List locally installed artifacts."""
    artifact_type = get_artifact_type(args)
    _, label_plural = get_artifact_labels(artifact_type)
    total_artifacts = 0

    check_updates = getattr(args, "check_updates", False)
    show_detail = args.detail or check_updates

    if args.target:
        target_dir = Path(args.target)
        if not target_dir.exists():
            log_warning(f"{label_plural.title()} directory not found: {target_dir}")
            return 1

        artifacts = discover_artifacts(target_dir, artifact_type=artifact_type)
        if artifacts:
            if check_updates:
                enrich_artifacts_with_update_status(artifacts)
            print(f"\n{Colors.BOLD}Installed {label_plural} (custom):{Colors.RESET}")
            print(f"  {Colors.YELLOW}Location: {target_dir}{Colors.RESET}")
            format_artifacts_list(artifacts, detailed=show_detail, show_source=show_detail)
            total_artifacts = len(artifacts)
        else:
            log_warning(f"No {label_plural} installed in {target_dir}")
        print(f"\n{Colors.GREEN}Total: {total_artifacts} {label_plural}{Colors.RESET}\n")
        return 0

    scopes_to_show = []

    if not args.project:
        global_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="personal")
        if global_dir.exists():
            global_artifacts = discover_artifacts(global_dir, artifact_type=artifact_type)
            if global_artifacts:
                scopes_to_show.append(("global", global_dir, global_artifacts))

    project_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="project")
    global_dir_check = get_claude_artifacts_dir(artifact_type=artifact_type, scope="personal")
    if project_dir != global_dir_check and project_dir.exists():
        project_artifacts = discover_artifacts(project_dir, artifact_type=artifact_type)
        if project_artifacts:
            scopes_to_show.append(("project", project_dir, project_artifacts))

    if not scopes_to_show:
        if args.project:
            log_warning(f"No project {label_plural} found in {project_dir}")
        else:
            log_warning(f"No {label_plural} installed")
        return 0

    for scope, directory, artifacts in scopes_to_show:
        if check_updates:
            enrich_artifacts_with_update_status(artifacts)
        scope_label = f"[{scope.upper()}]"
        print(f"\n{Colors.BOLD}{scope_label} Installed {label_plural}:{Colors.RESET}")
        print(f"  {Colors.YELLOW}Location: {directory}{Colors.RESET}")
        format_artifacts_list(artifacts, detailed=show_detail, show_source=show_detail)
        total_artifacts += len(artifacts)
        print()

    print(f"{Colors.GREEN}Total: {total_artifacts} {label_plural}{Colors.RESET}\n")
    return 0


def cmd_remove(args):
    """remove command: Remove installed artifacts."""
    artifact_type = get_artifact_type(args)
    label, label_plural = get_artifact_labels(artifact_type)

    target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope=get_scope(args))

    if args.target:
        target_dir = Path(args.target)

    if not target_dir.exists():
        log_warning(f"{label_plural.title()} directory not found: {target_dir}")
        return 1

    installed_artifacts = discover_artifacts(target_dir, artifact_type=artifact_type)

    if not installed_artifacts:
        log_warning(f"No {label_plural} installed in {target_dir}")
        return 0

    requested_items = getattr(args, "items", None) or getattr(args, "skills", None)

    if requested_items:
        requested = set(s.strip().lower() for s in requested_items.split(","))
        artifacts_to_remove = [
            s for s in installed_artifacts
            if s["folder_name"].lower() in requested
            or (s.get("name") and s["name"].lower() in requested)
        ]

        if not artifacts_to_remove:
            log_error(f"No matching {label_plural} found for: {requested_items}")
            log_info(f"Installed {label_plural}: " + ", ".join(s["folder_name"] for s in installed_artifacts))
            return 1
    elif args.all:
        artifacts_to_remove = installed_artifacts
    else:
        artifacts_to_remove = interactive_select(installed_artifacts, artifact_type=artifact_type)

    if not artifacts_to_remove:
        log_info(f"No {label_plural} selected")
        return 0

    dry_run = getattr(args, 'dry_run', False)

    if dry_run:
        print(f"\n{Colors.YELLOW}[DRY RUN] The following {label_plural} would be removed:{Colors.RESET}\n")
        for artifact in artifacts_to_remove:
            artifact_name = artifact.get("name") or artifact["folder_name"]
            artifact_path = artifact["path"]
            print(f"  {Colors.RED}-{Colors.RESET} {artifact_name}")
            print(f"    {Colors.YELLOW}Path: {artifact_path}{Colors.RESET}")
        print()
        log_info(f"[DRY RUN] Would remove {len(artifacts_to_remove)} {label_plural}")
        return 0

    if not args.force:
        names = [s.get("name") or s["folder_name"] for s in artifacts_to_remove]
        print(f"\n{Colors.YELLOW}The following {label_plural} will be removed:{Colors.RESET}")
        for name in names:
            print(f"  {Colors.RED}-{Colors.RESET} {name}")
        print()

        try:
            confirm = input(f"{Colors.BOLD}Confirm removal? [y/N]{Colors.RESET} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return 0

        if confirm not in ("y", "yes"):
            log_info("Cancelled")
            return 0

    removed = 0
    for artifact in artifacts_to_remove:
        artifact_path = artifact["path"]
        artifact_name = artifact.get("name") or artifact["folder_name"]
        try:
            shutil.rmtree(artifact_path)
            log_success(f"Removed: {artifact_name}")
            removed += 1
        except Exception as e:
            log_error(f"Failed to remove {artifact_name}: {e}")

    print()
    log_success(f"Removed {removed}/{len(artifacts_to_remove)} {label_plural}")
    return 0


def cmd_install(args):
    """install command: Install artifacts from a remote repo to local."""
    artifact_type = get_artifact_type(args)
    _, label_plural = get_artifact_labels(artifact_type)
    repo_info = prepare_repo_info(args)

    target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope=get_scope(args))

    if args.target:
        target_dir = Path(args.target)

    log_info(f"Target directory: {target_dir}")

    cloned_root = get_cloned_repo(repo_info, args)
    cache_dir = get_repo_cache_dir(repo_info)
    _, all_artifacts = find_artifacts_root(cloned_root, artifact_type=artifact_type)

    if not all_artifacts:
        log_error(f"No {label_plural} found in repository")
        return 1

    requested_items = getattr(args, "items", None) or getattr(args, "skills", None)

    if requested_items:
        requested = set(s.strip().lower() for s in requested_items.split(","))
        artifacts_to_install = [
            s for s in all_artifacts
            if s["folder_name"].lower() in requested
            or (s.get("name") and s["name"].lower() in requested)
        ]

        if not artifacts_to_install:
            log_error(f"No matching {label_plural} found for: {requested_items}")
            log_info(f"Available {label_plural}: " + ", ".join(s["folder_name"] for s in all_artifacts))
            return 1
    elif args.all:
        artifacts_to_install = all_artifacts
    else:
        artifacts_to_install = interactive_select(all_artifacts, artifact_type=artifact_type)

    if not artifacts_to_install:
        log_info(f"No {label_plural} selected")
        return 0

    commit_hash = get_git_commit_hash(cache_dir)

    dry_run = getattr(args, 'dry_run', False)
    backup = getattr(args, 'backup', False)

    if dry_run:
        print(f"\n{Colors.YELLOW}[DRY RUN] The following actions would be performed:{Colors.RESET}\n")

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    installed = 0
    skipped_external = 0
    for artifact in artifacts_to_install:
        artifact_name = artifact.get("name") or artifact["folder_name"]
        artifact_path = artifact.get("path")
        if artifact_path is None or not Path(artifact_path).exists():
            source_kind = artifact.get("source_kind", "external")
            log_warning(
                f"{artifact_name}: external plugin (source: {source_kind}), "
                f"not bundled in this repo — install from upstream instead"
            )
            skipped_external += 1
            continue
        success, message = install_artifact(
            artifact_path,
            target_dir,
            artifact_type=artifact_type,
            repo_info=repo_info,
            commit_hash=commit_hash,
            force=args.force,
            backup=backup,
            dry_run=dry_run
        )
        if success:
            if dry_run:
                print(f"  {Colors.CYAN}-{Colors.RESET} {artifact_name}: {message}")
            else:
                log_success(f"{artifact_name}: {message}")
            installed += 1
        else:
            log_warning(f"{artifact_name}: {message}")

    print()
    if dry_run:
        log_info(f"[DRY RUN] Would install {installed}/{len(artifacts_to_install)} {label_plural} to {target_dir}")
    else:
        log_success(f"Installed {installed}/{len(artifacts_to_install)} {label_plural} to {target_dir}")
    if skipped_external:
        log_info(f"Skipped {skipped_external} external {label_plural} (hosted in upstream repos)")
    return 0


def cmd_pack(args):
    """pack command: Pack artifacts into zip files."""
    artifact_type = get_artifact_type(args)
    _, label_plural = get_artifact_labels(artifact_type)
    repo_info = prepare_repo_info(args)
    output_dir = Path(args.output)

    cloned_root = get_cloned_repo(repo_info, args)
    _, all_artifacts = find_artifacts_root(cloned_root, artifact_type=artifact_type)

    if not all_artifacts:
        log_error(f"No {label_plural} found in repository")
        return 1

    requested_items = getattr(args, "items", None) or getattr(args, "skills", None)

    if requested_items:
        requested = set(s.strip().lower() for s in requested_items.split(","))
        artifacts_to_pack = [
            s for s in all_artifacts
            if s["folder_name"].lower() in requested
            or (s.get("name") and s["name"].lower() in requested)
        ]
    else:
        artifacts_to_pack = all_artifacts

    if not artifacts_to_pack:
        log_error(f"No matching {label_plural} found")
        return 1

    log_info(f"Packing {len(artifacts_to_pack)} {label_plural} to {output_dir}")

    packed_artifacts = []
    for artifact in artifacts_to_pack:
        artifact_path = artifact.get("path")
        artifact_name = artifact.get("name") or artifact["folder_name"]
        if artifact_path is None or not Path(artifact_path).exists():
            source_kind = artifact.get("source_kind", "external")
            log_warning(f"{artifact_name}: external {label_plural[:-1]} (source: {source_kind}) — skipping")
            continue
        pack_artifact(artifact_path, output_dir)
        packed_artifacts.append(artifact)
    artifacts_to_pack = packed_artifacts

    print()
    log_success(f"Packed {len(artifacts_to_pack)} {label_plural}")

    manifest = {
        artifact_type: [
            {
                "name": s.get("name") or s["folder_name"],
                "folder": s["folder_name"],
                "description": s.get("description"),
                "zip": f"{s['folder_name']}.zip"
            }
            for s in artifacts_to_pack
        ]
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    log_success(f"Generated manifest: {manifest_path}")

    return 0


def cmd_sync(args):
    """sync command: Sync artifacts from a remote repo."""
    artifact_type = get_artifact_type(args)
    _, label_plural = get_artifact_labels(artifact_type)
    repo_info = prepare_repo_info(args)

    target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope=get_scope(args))

    if args.target:
        target_dir = Path(args.target)

    log_info(f"Syncing {label_plural} to {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    cloned_root = get_cloned_repo(repo_info, args)
    _, artifacts = find_artifacts_root(cloned_root, artifact_type=artifact_type)

    if not artifacts:
        log_warning(f"No {label_plural} found in repository")
        return 1

    skipped_external = 0
    for artifact in artifacts:
        artifact_path = artifact.get("path")
        artifact_name = artifact.get("name") or artifact["folder_name"]
        if artifact_path is None or not Path(artifact_path).exists():
            source_kind = artifact.get("source_kind", "external")
            log_warning(f"{artifact_name}: external {label_plural[:-1]} (source: {source_kind}) — skipping")
            skipped_external += 1
            continue
        dest = target_dir / artifact_path.name
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        if artifact_path.is_file():
            shutil.copy2(artifact_path, dest)
        else:
            shutil.copytree(artifact_path, dest)
        log_success(f"Synced: {artifact_path.name}")

    log_success(f"{label_plural.title()} synced to {target_dir}")
    if skipped_external:
        log_info(f"Skipped {skipped_external} external {label_plural} (hosted in upstream repos)")

    _, artifacts = find_artifacts_root(target_dir, artifact_type=artifact_type)
    if artifacts:
        print(f"\n{Colors.BOLD}Installed {label_plural}:{Colors.RESET}")
        for artifact in artifacts:
            name = artifact.get("name") or artifact["folder_name"]
            print(f"  {Colors.CYAN}-{Colors.RESET} {name}")

    return 0


def cmd_update(args):
    """update command: Update installed artifacts to their latest upstream version."""
    artifact_type = get_artifact_type(args)
    _, label_plural = get_artifact_labels(artifact_type)

    target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope=get_scope(args))
    if args.target:
        target_dir = Path(args.target)

    if not target_dir.exists():
        log_error(f"No {label_plural} directory found: {target_dir}")
        return 1

    all_installed = discover_artifacts(target_dir, artifact_type=artifact_type)
    if not all_installed:
        log_warning(f"No {label_plural} installed in {target_dir}")
        return 0

    requested_items = getattr(args, "items", None)

    if requested_items:
        requested = set(n.strip().lower() for n in requested_items.split(","))
        candidates = [
            a for a in all_installed
            if a["folder_name"].lower() in requested
            or (a.get("name") and a["name"].lower() in requested)
        ]
        if not candidates:
            log_error(f"No matching {label_plural} found: {requested_items}")
            return 1
    elif args.all:
        candidates = all_installed
    else:
        candidates = interactive_select(all_installed, artifact_type=artifact_type)

    if not candidates:
        log_info(f"No {label_plural} selected")
        return 0

    # Filter to those with source metadata; warn on the rest
    to_update = []
    for artifact in candidates:
        meta = read_artifact_metadata(artifact["path"])
        if not meta or not meta.get("source_url"):
            name = artifact.get("name") or artifact["folder_name"]
            log_warning(f"{name}: no install metadata (.agentkit.json) — install via agentkit to enable updates")
            continue
        artifact["_meta"] = meta
        to_update.append(artifact)

    if not to_update:
        log_warning(f"No updatable {label_plural} found")
        return 0

    # Group by source_url to batch repo fetches
    groups: dict[str, list[dict]] = {}
    for artifact in to_update:
        url = artifact["_meta"]["source_url"]
        groups.setdefault(url, []).append(artifact)

    dry_run = getattr(args, "dry_run", False)
    backup = getattr(args, "backup", False)

    if dry_run:
        print(f"\n{Colors.YELLOW}[DRY RUN] The following actions would be performed:{Colors.RESET}\n")

    updated = 0
    skipped = 0

    for source_url, artifacts in groups.items():
        repo_info = parse_repo_url(source_url)
        force_refresh = getattr(args, "no_cache", False)
        cache_dir = get_repo_cache_dir(repo_info)
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            cloned_root = clone_repo(repo_info, cache_dir, force_refresh=force_refresh)
        except Exception as e:
            for artifact in artifacts:
                name = artifact.get("name") or artifact["folder_name"]
                log_error(f"{name}: failed to fetch source — {e}")
                skipped += 1
            continue

        _, remote_artifacts = find_artifacts_root(cloned_root, artifact_type=artifact_type)
        commit_hash = get_git_commit_hash(cache_dir)

        remote_by_key: dict[str, dict] = {}
        for ra in remote_artifacts:
            remote_by_key[ra["folder_name"].lower()] = ra
            if ra.get("name"):
                remote_by_key[ra["name"].lower()] = ra

        for artifact in artifacts:
            artifact_name = artifact.get("name") or artifact["folder_name"]
            remote = remote_by_key.get(artifact["folder_name"].lower()) or remote_by_key.get(
                (artifact.get("name") or "").lower()
            )
            if not remote:
                log_warning(f"{artifact_name}: not found in upstream — skipping")
                skipped += 1
                continue

            artifact_path = remote.get("path")
            if artifact_path is None or not Path(artifact_path).exists():
                log_warning(f"{artifact_name}: external artifact — skipping")
                skipped += 1
                continue

            success, message = install_artifact(
                artifact_path,
                target_dir,
                artifact_type=artifact_type,
                repo_info=repo_info,
                commit_hash=commit_hash,
                force=True,
                backup=backup,
                dry_run=dry_run,
            )
            if success:
                if dry_run:
                    print(f"  {Colors.CYAN}-{Colors.RESET} {artifact_name}: {message}")
                else:
                    log_success(f"{artifact_name}: {message}")
                updated += 1
            else:
                log_warning(f"{artifact_name}: {message}")
                skipped += 1

    print()
    if dry_run:
        log_info(f"[DRY RUN] Would update {updated}/{len(to_update)} {label_plural}")
    else:
        log_success(f"Updated {updated}/{len(to_update)} {label_plural}")
    if skipped:
        log_info(f"Skipped {skipped} {label_plural}")
    return 0


def cmd_validate(args):
    """validate command: Validate artifact marker format."""
    artifact_type = get_artifact_type(args)
    label, label_plural = get_artifact_labels(artifact_type)

    def do_validate(artifacts_to_validate: list[dict]) -> int:
        """Execute the actual validation logic."""
        if not artifacts_to_validate:
            log_warning(f"No {label_plural} to validate")
            return 1

        print(f"\n{Colors.BOLD}Validating {len(artifacts_to_validate)} {label_plural}...{Colors.RESET}\n")

        total_issues = 0
        for artifact in artifacts_to_validate:
            artifact_name = artifact.get("name") or artifact["folder_name"]
            artifact_path = artifact.get("path")
            if artifact_path is None or not Path(artifact_path).exists():
                source_kind = artifact.get("source_kind", "external")
                print(f"  {Colors.YELLOW}○{Colors.RESET} {artifact_name} (external: {source_kind}) — skipped")
                continue
            issues = validate_artifact_md(artifact_path, artifact_type=artifact_type)

            if issues:
                print(f"  {Colors.RED}✗{Colors.RESET} {Colors.BOLD}{artifact_name}{Colors.RESET}")
                for issue in issues:
                    print(f"    {Colors.YELLOW}-{Colors.RESET} {issue}")
                total_issues += len(issues)
            else:
                print(f"  {Colors.GREEN}✓{Colors.RESET} {artifact_name}")

        print()
        if total_issues > 0:
            log_warning(f"Found {total_issues} issues in {len(artifacts_to_validate)} {label_plural}")
            return 1
        else:
            log_success(f"All {len(artifacts_to_validate)} {label_plural} are valid")
            return 0

    # Get skills to validate based on source
    if args.path:
        artifact_path = Path(args.path)
        if artifact_path.is_file():
            artifact_path = artifact_path.parent
        artifacts_to_validate = [{"path": artifact_path, "folder_name": artifact_path.name}]
        return do_validate(artifacts_to_validate)

    elif args.repo:
        repo_info = prepare_repo_info(args)
        cloned_root = get_cloned_repo(repo_info, args)
        _, artifacts_to_validate = find_artifacts_root(cloned_root, artifact_type=artifact_type)
        return do_validate(artifacts_to_validate)

    else:
        # Validate locally installed skills
        if args.project:
            target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="project")
        else:
            target_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="personal")
        artifacts_to_validate = discover_artifacts(target_dir, artifact_type=artifact_type)
        return do_validate(artifacts_to_validate)


def cmd_doctor(args):
    """doctor command: Diagnose artifact directory structure."""
    artifact_type = get_artifact_type(args)
    label, label_plural = get_artifact_labels(artifact_type)
    marker_file = ARTIFACT_CONFIG[artifact_type]["marker"]

    issues = []
    warnings = []

    print(f"\n{Colors.BOLD}Artifacts CLI Doctor ({label_plural}){Colors.RESET}\n")

    global_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="personal")
    print(f"  {Colors.CYAN}Global {label_plural}:{Colors.RESET} {global_dir}")
    if global_dir.exists():
        global_artifacts = discover_artifacts(global_dir, artifact_type=artifact_type)
        print(f"    {Colors.GREEN}✓{Colors.RESET} Directory exists ({len(global_artifacts)} {label_plural})")

        for artifact in global_artifacts:
            artifact_issues = validate_artifact_md(artifact["path"], artifact_type=artifact_type)
            if artifact_issues:
                warnings.append(f"Global {label} '{artifact['folder_name']}' has issues")
    else:
        print(f"    {Colors.YELLOW}⚠{Colors.RESET} Directory does not exist")

    project_dir = get_claude_artifacts_dir(artifact_type=artifact_type, scope="project")
    print(f"\n  {Colors.CYAN}Project {label_plural}:{Colors.RESET} {project_dir}")
    if project_dir.exists():
        project_artifacts = discover_artifacts(project_dir, artifact_type=artifact_type)
        print(f"    {Colors.GREEN}✓{Colors.RESET} Directory exists ({len(project_artifacts)} {label_plural})")

        for artifact in project_artifacts:
            artifact_issues = validate_artifact_md(artifact["path"], artifact_type=artifact_type)
            if artifact_issues:
                warnings.append(f"Project {label} '{artifact['folder_name']}' has issues")
    else:
        print(f"    {Colors.YELLOW}○{Colors.RESET} Directory does not exist (this is normal)")

    print(f"\n  {Colors.CYAN}Checking for orphaned directories...{Colors.RESET}")
    orphaned = []
    for artifacts_dir in [global_dir, project_dir]:
        if artifacts_dir.exists():
            for item in artifacts_dir.iterdir():
                if item.is_dir() and not item.name.startswith("."):
                    marker_path = item / marker_file
                    if not marker_path.exists():
                        orphaned.append(item)
                        issues.append(f"Orphaned directory (no {marker_file}): {item}")

    if orphaned:
        for item in orphaned:
            print(f"    {Colors.RED}✗{Colors.RESET} {item.name} (no {marker_file})")
    else:
        print(f"    {Colors.GREEN}✓{Colors.RESET} No orphaned directories")

    print(f"\n  {Colors.CYAN}Checking for backup directories...{Colors.RESET}")
    backup_dirs = []
    for artifacts_dir in [global_dir, project_dir]:
        backup_dir = artifacts_dir / ".backup"
        if backup_dir.exists():
            backup_count = len(list(backup_dir.iterdir()))
            backup_dirs.append((backup_dir, backup_count))
            print(f"    {Colors.YELLOW}○{Colors.RESET} {backup_dir} ({backup_count} backups)")

    if not backup_dirs:
        print(f"    {Colors.GREEN}✓{Colors.RESET} No backup directories")

    if getattr(args, "check_updates", False):
        print(f"\n  {Colors.CYAN}Checking for available updates...{Colors.RESET}")
        all_installed = []
        for artifacts_dir in [global_dir, project_dir]:
            if artifacts_dir.exists():
                all_installed.extend(discover_artifacts(artifacts_dir, artifact_type=artifact_type))

        outdated = []
        no_meta = []
        for artifact in all_installed:
            name = artifact.get("name") or artifact["folder_name"]
            meta = read_artifact_metadata(artifact.get("path"))
            if not meta or not meta.get("clone_url") or not meta.get("commit"):
                no_meta.append(name)
                print(f"    {Colors.YELLOW}○{Colors.RESET} {name}: no install metadata — skipped")
                continue
            remote_sha = get_remote_head_commit(meta["clone_url"], meta.get("branch", "main"))
            if remote_sha is None:
                print(f"    {Colors.YELLOW}?{Colors.RESET} {name}: could not reach upstream")
                warnings.append(f"{name}: upstream unreachable during update check")
            elif not remote_sha.startswith(meta["commit"]):
                remote_short = remote_sha[:7]
                print(f"    {Colors.RED}⬆{Colors.RESET} {name}: update available  (installed: {meta['commit']}  upstream: {remote_short})")
                outdated.append(name)
                warnings.append(f"{name}: update available (run: agentkit update --items {artifact['folder_name']})")
            else:
                print(f"    {Colors.GREEN}✓{Colors.RESET} {name}: up to date ({meta['commit']})")

        if not all_installed:
            print(f"    {Colors.YELLOW}○{Colors.RESET} No installed {label_plural} found")

    print(f"\n{Colors.BOLD}Summary:{Colors.RESET}")
    if issues:
        print(f"  {Colors.RED}✗{Colors.RESET} {len(issues)} issues found")
        for issue in issues:
            print(f"    - {issue}")
    else:
        print(f"  {Colors.GREEN}✓{Colors.RESET} No issues found")

    if warnings:
        print(f"  {Colors.YELLOW}⚠{Colors.RESET} {len(warnings)} warnings")
        for warning in warnings:
            print(f"    - {warning}")

    print()
    return 1 if issues else 0


def add_type_argument(parser: argparse.ArgumentParser, include_all: bool = False, default: str = "skills") -> None:
    """Add shared --type argument to subcommands."""
    choices = sorted(ARTIFACT_CONFIG.keys())
    if include_all:
        choices = ["all"] + choices
    parser.add_argument(
        "--type",
        "-T",
        choices=choices,
        default=default,
        help=f"Artifact type to manage (default: {default})",
    )


def add_items_argument(parser: argparse.ArgumentParser, action: str) -> None:
    """Add shared --items argument while preserving --skills compatibility."""
    parser.add_argument(
        "--items",
        help=f"Comma-separated list of items to {action}",
    )


def expand_typed_command_alias(argv: list[str]) -> list[str]:
    """Expand shorthand command alias like 'commands-list' to 'list --type commands'."""
    if not argv:
        return argv

    match = re.match(
        r"^(skills|commands|agents|plugins)-(list|installed|remove|install|pack|sync|update|validate|doctor)$",
        argv[0],
    )
    if not match:
        return argv

    artifact_type, command = match.groups()
    return [command, "--type", artifact_type, *argv[1:]]


def expand_resource_style_command(argv: list[str]) -> list[str]:
    """Expand kubectl-style 'resource verb' syntax to 'verb --type resource'."""
    if len(argv) < 2:
        return argv

    resource_aliases = {
        "skill": "skills",
        "skills": "skills",
        "command": "commands",
        "commands": "commands",
        "agent": "agents",
        "agents": "agents",
        "plugin": "plugins",
        "plugins": "plugins",
    }
    verbs = {"list", "installed", "remove", "install", "pack", "sync", "update", "validate", "doctor"}

    resource = resource_aliases.get(argv[0].strip().lower())
    verb = argv[1].strip().lower()

    if resource and verb in verbs:
        return [verb, "--type", resource, *argv[2:]]

    return argv


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    """CLI program entry point."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="agentkit",
        description="Cross-platform CLI for managing Claude Code artifacts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List — three equivalent forms
  agentkit agents list                             # <type> <verb>
  agentkit list agents                             # <verb> <type>
  agentkit list --type agents                      # flag form

  agentkit skills list https://github.com/your-org/your-repo
  agentkit list skills https://github.com/your-org/your-repo
  agentkit list https://github.com/your-org/your-repo --type skills

  # Install — three equivalent forms
  agentkit agents install --items my-agent
  agentkit install agents --items my-agent
  agentkit install --type agents --items my-agent

  agentkit plugins install https://github.com/your-org/your-repo --all --scope local
  agentkit install plugins https://github.com/your-org/your-repo --all --scope local
  agentkit install https://github.com/your-org/your-repo --type plugins --all --scope local

  # Remove — three equivalent forms
  agentkit agents remove --all
  agentkit remove agents --all
  agentkit remove --type agents --all

  # Other commands
  agentkit installed --detail
  agentkit agents installed
  agentkit update --all
  agentkit sync
  agentkit pack --output dist/desktop
  agentkit validate
  agentkit doctor
"""
    )

    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # List command
    list_parser = subparsers.add_parser(
        "list",
        help="List available artifacts from a repository",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Syntax:
  agentkit <type> list [<url>]                     # <type> <verb>  (kubectl-style)
  agentkit list <type> [<url>]                     # <verb> <type>
  agentkit list [<url>] --type <type>              # flag form

Examples:
  agentkit list                                    # all types, default repo
  agentkit agents list                             # agents only
  agentkit list agents                             #   equivalent
  agentkit list --type agents                      #   equivalent
  agentkit skills list https://github.com/your-org/your-repo
  agentkit list skills https://github.com/your-org/your-repo
  agentkit list https://github.com/your-org/your-repo --type skills
  agentkit list https://github.com/your-org/your-repo
  agentkit list --detail
  agentkit list --no-cache
"""
    )
    list_parser.add_argument("positionals", nargs="*",
                             help="[type] [url]  e.g. 'agents https://github.com/org/repo'")
    list_parser.add_argument("--repo", "-r", default=DEFAULT_REPO,
                             help="Repository URL (default: Anthropic official)")
    list_parser.add_argument("--branch", "-b", help="Git branch (default: auto-detect or main)")
    list_parser.add_argument("--detail", "-d", action="store_true",
                             help="Show detailed info (name and description)")
    add_type_argument(list_parser, include_all=True, default="all")
    list_parser.add_argument("--no-cache", action="store_true", help="Force fresh clone, ignoring local cache")
    list_parser.set_defaults(func=cmd_list)

    # Installed command
    installed_parser = subparsers.add_parser("installed", help="List installed artifacts")
    installed_parser.add_argument("--project", "-p", action="store_true",
                                  help="Show project artifacts (.claude/<type>/)")
    installed_parser.add_argument("--target", "-t", help="Custom artifact directory")
    installed_parser.add_argument("--detail", "-d", action="store_true",
                                  help="Show detailed info (name, description, version)")
    installed_parser.add_argument("--check-updates", "-u", action="store_true",
                                  help="Check each artifact against its upstream and show update status (implies --detail)")
    add_type_argument(installed_parser)
    installed_parser.set_defaults(func=cmd_installed)

    # Remove command
    remove_parser = subparsers.add_parser(
        "remove",
        aliases=["uninstall"],
        help="Remove installed artifacts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Syntax:
  agentkit <type> remove [options]                 # <type> <verb>  (kubectl-style)
  agentkit remove <type> [options]                 # <verb> <type>
  agentkit remove --type <type> [options]          # flag form

Examples:
  agentkit remove                                  # interactive selection
  agentkit remove --items my-skill
  agentkit agents remove --all
  agentkit remove agents --all                     #   equivalent
  agentkit remove --type agents --all              #   equivalent
  agentkit skills remove --all --scope local
  agentkit remove skills --all --scope local
  agentkit remove --all --dry-run
  agentkit remove --all --force
"""
    )
    remove_parser.add_argument("--skills", "-s", help="Comma-separated list of skills to remove (legacy alias)")
    add_items_argument(remove_parser, action="remove")
    remove_parser.add_argument("--all", "-a", action="store_true", help="Remove all artifacts of selected type")
    remove_parser.add_argument("--scope", choices=["global", "local"], default="global",
                               help="Config scope: global (~/.claude/) or local (.claude/ in project) (default: global)")
    remove_parser.add_argument("--project", "-p", action="store_true", help=argparse.SUPPRESS)
    remove_parser.add_argument("--target", "-t", help="Custom artifact directory")
    remove_parser.add_argument("--force", "-f", action="store_true",
                               help="Skip confirmation prompt")
    remove_parser.add_argument("--dry-run", action="store_true",
                               help="Show what would be removed without actually removing")
    add_type_argument(remove_parser)
    remove_parser.set_defaults(func=cmd_remove)

    # Install command
    install_parser = subparsers.add_parser(
        "install",
        help="Install artifacts from a repository",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Syntax:
  agentkit <type> install [<url>] [options]        # <type> <verb>  (kubectl-style)
  agentkit install <type> [<url>] [options]        # <verb> <type>
  agentkit install [<url>] --type <type> [options] # flag form

Examples:
  agentkit install                                 # interactive, default repo
  agentkit install --all                           # install all, default repo
  agentkit agents install --items my-agent
  agentkit install agents --items my-agent         #   equivalent
  agentkit install --type agents --items my-agent  #   equivalent
  agentkit plugins install https://github.com/your-org/your-repo --all --scope local
  agentkit install plugins https://github.com/your-org/your-repo --all --scope local
  agentkit install https://github.com/your-org/your-repo --type plugins --all --scope local
  agentkit install --all --dry-run
  agentkit install --all --force --backup
"""
    )
    install_parser.add_argument("repo_url", nargs="?", default=None,
                                help="Repository URL (positional, e.g., https://github.com/user/repo)")
    install_parser.add_argument("--repo", "-r", default=DEFAULT_REPO,
                                help=f"Repository URL (default: Anthropic official)")
    install_parser.add_argument("--branch", "-b", help="Git branch (default: auto-detect or main)")
    install_parser.add_argument("--skills", "-s", help="Comma-separated list of skills to install (legacy alias)")
    add_items_argument(install_parser, action="install")
    install_parser.add_argument("--all", "-a", action="store_true", help="Install all artifacts of selected type")
    install_parser.add_argument("--scope", choices=["global", "local"], default="global",
                                help="Config scope: global (~/.claude/) or local (.claude/ in project) (default: global)")
    install_parser.add_argument("--project", "-p", action="store_true", help=argparse.SUPPRESS)
    install_parser.add_argument("--target", "-t", help="Custom target directory")
    install_parser.add_argument("--force", "-f", action="store_true", help="Overwrite existing artifacts")
    install_parser.add_argument("--backup", action="store_true",
                                help="Backup existing artifacts before overwriting")
    install_parser.add_argument("--dry-run", action="store_true",
                                help="Show what would be installed without actually installing")
    add_type_argument(install_parser)
    install_parser.add_argument("--no-cache", action="store_true", help="Force fresh clone, ignoring local cache")
    install_parser.set_defaults(func=cmd_install)

    # Pack command
    pack_parser = subparsers.add_parser("pack", help="Pack artifacts into zip files for Claude Desktop")
    pack_parser.add_argument("repo_url", nargs="?", default=None,
                             help="Repository URL (positional, e.g., https://github.com/user/repo)")
    pack_parser.add_argument("--repo", "-r", default=DEFAULT_REPO,
                             help=f"Repository URL (default: Anthropic official)")
    pack_parser.add_argument("--branch", "-b", help="Git branch (default: auto-detect or main)")
    pack_parser.add_argument("--skills", "-s", help="Comma-separated list of skills to pack (legacy alias)")
    add_items_argument(pack_parser, action="pack")
    pack_parser.add_argument("--output", "-o", default="dist/desktop", help="Output directory")
    add_type_argument(pack_parser)
    pack_parser.add_argument("--no-cache", action="store_true", help="Force fresh clone, ignoring local cache")
    pack_parser.set_defaults(func=cmd_pack)

    # Sync command
    sync_parser = subparsers.add_parser("sync", help="Sync artifacts from a repository")
    sync_parser.add_argument("repo_url", nargs="?", default=None,
                             help="Repository URL (positional, e.g., https://github.com/user/repo)")
    sync_parser.add_argument("--repo", "-r", default=DEFAULT_REPO,
                             help=f"Repository URL (default: Anthropic official)")
    sync_parser.add_argument("--branch", "-b", help="Git branch (default: auto-detect or main)")
    sync_parser.add_argument("--project", "-p", action="store_true",
                             help="Sync to project .claude/<type>/")
    sync_parser.add_argument("--target", "-t", help="Custom target directory")
    add_type_argument(sync_parser)
    sync_parser.add_argument("--no-cache", action="store_true", help="Force fresh clone, ignoring local cache")
    sync_parser.set_defaults(func=cmd_sync)

    # Update command
    update_parser = subparsers.add_parser(
        "update",
        help="Update installed artifacts to their latest upstream version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Syntax:
  agentkit <type> update [options]                 # <type> <verb>  (kubectl-style)
  agentkit update <type> [options]                 # <verb> <type>
  agentkit update --type <type> [options]          # flag form

Examples:
  agentkit update                                  # interactive, updates installed skills
  agentkit update --all                            # update all installed skills
  agentkit update --items my-skill,other-skill     # update specific skills
  agentkit agents update --all                     # update all installed agents
  agentkit update --all --backup                   # backup before overwriting
  agentkit update --all --dry-run                  # preview what would be updated
  agentkit update --all --no-cache                 # force fresh upstream fetch
  agentkit update --all --scope local              # update project-local artifacts
"""
    )
    add_items_argument(update_parser, action="update")
    update_parser.add_argument("--all", "-a", action="store_true", help="Update all installed artifacts of selected type")
    update_parser.add_argument("--scope", choices=["global", "local"], default="global",
                               help="Config scope: global (~/.claude/) or local (.claude/ in project) (default: global)")
    update_parser.add_argument("--project", "-p", action="store_true", help=argparse.SUPPRESS)
    update_parser.add_argument("--target", "-t", help="Custom artifact directory")
    update_parser.add_argument("--backup", action="store_true",
                               help="Backup existing artifacts before overwriting")
    update_parser.add_argument("--dry-run", action="store_true",
                               help="Show what would be updated without actually updating")
    add_type_argument(update_parser)
    update_parser.add_argument("--no-cache", action="store_true", help="Force fresh clone, ignoring local cache")
    update_parser.set_defaults(func=cmd_update)

    # Validate command
    validate_parser = subparsers.add_parser("validate", help="Validate artifact marker format")
    validate_parser.add_argument("--path", help="Path to artifact directory or marker file")
    validate_parser.add_argument("--repo", "-r", help="Validate artifacts from a repository")
    validate_parser.add_argument("--branch", "-b", help="Git branch for --repo")
    validate_parser.add_argument("--project", "-p", action="store_true",
                                 help="Validate project artifacts")
    add_type_argument(validate_parser)
    validate_parser.set_defaults(func=cmd_validate)

    # Doctor command
    doctor_parser = subparsers.add_parser("doctor", help="Diagnose artifact directory issues")
    add_type_argument(doctor_parser)
    doctor_parser.add_argument("--check-updates", "-u", action="store_true",
                               help="Check each installed artifact against its upstream for available updates")
    doctor_parser.set_defaults(func=cmd_doctor)

    argv = expand_typed_command_alias(sys.argv[1:])
    argv = expand_resource_style_command(argv)
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as e:
        log_error(f"Error: {e}")
        if os.environ.get("DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
