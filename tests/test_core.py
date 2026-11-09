"""
Unit tests for agentkit core functions.

These tests ensure the core logic works correctly before and after refactoring.
Run with: python -m pytest tests/ -v
"""

import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agentkit import (
    parse_repo_url,
    ARTIFACT_CONFIG,
    discover_artifacts,
    find_artifacts_root,
    get_claude_artifacts_dir,
    validate_artifact_md,
    discover_skills,
    find_skills_root,
    parse_skill_md,
    validate_skill_md,
    COMMON_SKILL_DIRS,
)


class TestParseRepoUrl:
    """Tests for parse_repo_url function."""

    def test_github_tree_url_with_subdir(self):
        """Parse GitHub tree URL with subdirectory."""
        url = "https://github.com/anthropics/skills/tree/main/skills"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://github.com/anthropics/skills.git"
        assert result["branch"] == "main"
        assert result["subdir"] == "skills"
        assert result["host"] == "github"

    def test_github_tree_url_without_subdir(self):
        """Parse GitHub tree URL without subdirectory."""
        url = "https://github.com/user/repo/tree/develop"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://github.com/user/repo.git"
        assert result["branch"] == "develop"
        assert result["subdir"] is None
        assert result["host"] == "github"

    def test_github_tree_url_with_nested_subdir(self):
        """Parse GitHub tree URL with nested subdirectory."""
        url = "https://github.com/org/repo/tree/main/packages/skills/claude"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://github.com/org/repo.git"
        assert result["branch"] == "main"
        assert result["subdir"] == "packages/skills/claude"

    def test_gitlab_tree_url(self):
        """Parse GitLab tree URL."""
        url = "https://gitlab.com/company/team-repo/-/tree/main/skills"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://gitlab.com/company/team-repo.git"
        assert result["branch"] == "main"
        assert result["subdir"] == "skills"
        assert result["host"] == "gitlab"

    def test_plain_https_url(self):
        """Parse plain HTTPS URL (repo homepage)."""
        url = "https://github.com/user/repo"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://github.com/user/repo.git"
        assert result["branch"] == "main"  # default
        assert result["subdir"] is None

    def test_https_url_with_git_suffix(self):
        """Parse HTTPS URL that already has .git suffix."""
        url = "https://github.com/user/repo.git"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://github.com/user/repo.git"

    def test_ssh_url(self):
        """Parse SSH URL."""
        url = "git@github.com:user/repo.git"
        result = parse_repo_url(url)

        assert result["clone_url"] == "git@github.com:user/repo.git"
        assert result["host"] == "github.com"

    def test_ssh_url_without_git_suffix(self):
        """Parse SSH URL without .git suffix."""
        url = "git@github.com:user/repo"
        result = parse_repo_url(url)

        assert result["clone_url"] == "git@github.com:user/repo.git"

    def test_self_hosted_gitlab(self):
        """Parse self-hosted GitLab URL."""
        url = "https://git.company.com/team/project/-/tree/develop/skills"
        result = parse_repo_url(url)

        assert result["clone_url"] == "https://git.company.com/team/project.git"
        assert result["branch"] == "develop"
        assert result["subdir"] == "skills"


class TestDiscoverSkills:
    """Tests for discover_skills function."""

    def test_discover_skills_in_directory(self):
        """Discover skills in a directory with SKILL.md files."""
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)

            # Create skill directories
            (skills_dir / "pdf").mkdir()
            (skills_dir / "pdf" / "SKILL.md").write_text(
                "---\nname: PDF Tool\ndescription: Work with PDFs\n---\nContent"
            )

            (skills_dir / "xlsx").mkdir()
            (skills_dir / "xlsx" / "SKILL.md").write_text(
                "---\nname: Excel Tool\ndescription: Work with Excel\n---\nContent"
            )

            skills = discover_skills(skills_dir)

            assert len(skills) == 2
            names = [s.get("name") for s in skills]
            assert "PDF Tool" in names
            assert "Excel Tool" in names

    def test_discover_skills_empty_directory(self):
        """Return empty list for directory without skills."""
        with tempfile.TemporaryDirectory() as tmp:
            skills = discover_skills(Path(tmp))
            assert skills == []

    def test_discover_skills_ignores_files(self):
        """Ignore files (only look at directories)."""
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)

            # Create a file, not a directory
            (skills_dir / "README.md").write_text("# README")

            skills = discover_skills(skills_dir)
            assert skills == []

    def test_discover_skills_ignores_dirs_without_skill_md(self):
        """Ignore directories without SKILL.md."""
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp)

            # Create directory without SKILL.md
            (skills_dir / "not-a-skill").mkdir()
            (skills_dir / "not-a-skill" / "README.md").write_text("# Not a skill")

            skills = discover_skills(skills_dir)
            assert skills == []

    def test_discover_skills_nonexistent_directory(self):
        """Return empty list for nonexistent directory."""
        skills = discover_skills(Path("/nonexistent/path"))
        assert skills == []


class TestDiscoverArtifacts:
    """Tests for discover_artifacts function."""

    def test_discover_commands_in_directory(self):
        """Discover commands using COMMAND.md marker files."""
        with tempfile.TemporaryDirectory() as tmp:
            commands_dir = Path(tmp)

            (commands_dir / "build").mkdir()
            (commands_dir / "build" / "COMMAND.md").write_text(
                "---\nname: Build\ndescription: Build project\n---\nContent"
            )

            commands = discover_artifacts(commands_dir, artifact_type="commands")

            assert len(commands) == 1
            assert commands[0].get("name") == "Build"


class TestFindSkillsRoot:
    """Tests for find_skills_root function."""

    def test_find_skills_in_root(self):
        """Find skills when they are in root directory."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)

            # Create skill directly in root
            (repo_root / "my-skill").mkdir()
            (repo_root / "my-skill" / "SKILL.md").write_text(
                "---\nname: My Skill\ndescription: Test\n---\nContent"
            )

            skills_root, skills = find_skills_root(repo_root)

            assert skills_root == repo_root
            assert len(skills) == 1

    def test_find_skills_in_common_subdir(self):
        """Find skills in common subdirectory (skills/)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)

            # Create skills in 'skills/' subdirectory
            (repo_root / "skills").mkdir()
            (repo_root / "skills" / "pdf").mkdir()
            (repo_root / "skills" / "pdf" / "SKILL.md").write_text(
                "---\nname: PDF\ndescription: Test\n---\nContent"
            )

            skills_root, skills = find_skills_root(repo_root)

            assert skills_root == repo_root / "skills"
            assert len(skills) == 1

    def test_find_skills_searches_multiple_common_dirs(self):
        """Search multiple common directory names."""
        for common_dir in COMMON_SKILL_DIRS[:3]:  # Test first few
            with tempfile.TemporaryDirectory() as tmp:
                repo_root = Path(tmp)

                # Create skills in this common directory
                skill_dir = repo_root / common_dir / "test-skill"
                skill_dir.mkdir(parents=True)
                (skill_dir / "SKILL.md").write_text(
                    "---\nname: Test\ndescription: Test\n---\nContent"
                )

                skills_root, skills = find_skills_root(repo_root)

                assert len(skills) == 1
                assert skills[0]["folder_name"] == "test-skill"

    def test_find_skills_deep_search(self):
        """Find skills in nested directory structure."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)

            # Create skill in unusual location
            (repo_root / "packages" / "tools" / "my-skill").mkdir(parents=True)
            (repo_root / "packages" / "tools" / "my-skill" / "SKILL.md").write_text(
                "---\nname: Deep Skill\ndescription: Test\n---\nContent"
            )

            skills_root, skills = find_skills_root(repo_root)

            assert len(skills) == 1
            assert skills[0].get("name") == "Deep Skill"


class TestFindArtifactsRoot:
    """Tests for find_artifacts_root function."""

    def test_find_agents_in_common_subdir(self):
        """Find agents in common subdirectory (agents/)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)

            (repo_root / "agents" / "reviewer").mkdir(parents=True)
            (repo_root / "agents" / "reviewer" / "AGENT.md").write_text(
                "---\nname: Reviewer\ndescription: Reviews code\n---\nContent"
            )

            root, agents = find_artifacts_root(repo_root, artifact_type="agents")

            assert root == repo_root / "agents"
            assert len(agents) == 1
            assert agents[0]["folder_name"] == "reviewer"


class TestParseSkillMd:
    """Tests for parse_skill_md function."""

    def test_parse_complete_frontmatter(self):
        """Parse SKILL.md with complete frontmatter."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text(
                "---\nname: My Skill\ndescription: A great skill\n---\nContent here"
            )

            result = parse_skill_md(skill_md)

            assert result["name"] == "My Skill"
            assert result["description"] == "A great skill"

    def test_parse_frontmatter_with_quotes(self):
        """Parse frontmatter with quoted values."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text(
                '---\nname: "Quoted Name"\ndescription: \'Single quoted\'\n---\nContent'
            )

            result = parse_skill_md(skill_md)

            assert result["name"] == "Quoted Name"
            assert result["description"] == "Single quoted"

    def test_parse_missing_frontmatter(self):
        """Return None values when frontmatter is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("# Just content\nNo frontmatter here")

            result = parse_skill_md(skill_md)

            assert result["name"] is None
            assert result["description"] is None

    def test_parse_partial_frontmatter(self):
        """Parse frontmatter with only some fields."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\nname: Only Name\n---\nContent")

            result = parse_skill_md(skill_md)

            assert result["name"] == "Only Name"
            assert result["description"] is None


class TestValidateSkillMd:
    """Tests for validate_skill_md function."""

    def test_valid_skill(self):
        """Valid skill should return no issues."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Valid Skill\ndescription: A valid skill\n---\nContent body"
            )

            issues = validate_skill_md(skill_dir)

            assert issues == []

    def test_missing_skill_md(self):
        """Missing SKILL.md should be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            # No SKILL.md file

            issues = validate_skill_md(skill_dir)

            assert len(issues) == 1
            assert "Missing SKILL.md" in issues[0]

    def test_missing_frontmatter(self):
        """Missing frontmatter should be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            (skill_dir / "SKILL.md").write_text("# No frontmatter\nJust content")

            issues = validate_skill_md(skill_dir)

            assert any("frontmatter" in issue.lower() for issue in issues)

    def test_missing_required_fields(self):
        """Missing required fields should be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Only Name\n---\nContent"
            )

            issues = validate_skill_md(skill_dir)

            assert any("description" in issue.lower() for issue in issues)

    def test_empty_body(self):
        """Empty body after frontmatter should be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Test\ndescription: Test\n---\n"
            )

            issues = validate_skill_md(skill_dir)

            assert any("empty" in issue.lower() for issue in issues)

    def test_name_too_long(self):
        """Name exceeding 50 characters should be reported."""
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            long_name = "A" * 60
            (skill_dir / "SKILL.md").write_text(
                f"---\nname: {long_name}\ndescription: Test\n---\nContent"
            )

            issues = validate_skill_md(skill_dir)

            assert any("name" in issue.lower() and "long" in issue.lower() for issue in issues)


class TestValidateArtifactMd:
    """Tests for validate_artifact_md function."""

    def test_missing_marker_for_plugin(self):
        """Missing PLUGIN.md should be reported for plugins."""
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp)
            issues = validate_artifact_md(plugin_dir, artifact_type="plugins")

            assert len(issues) == 1
            assert "PLUGIN.md" in issues[0]


class TestArtifactPaths:
    """Tests for artifact path helpers."""

    def test_artifact_config_contains_expected_types(self):
        """Artifact config should include all supported types."""
        assert set(ARTIFACT_CONFIG.keys()) == {"skills", "commands", "agents", "plugins"}

    def test_project_artifact_dir(self):
        """Project artifact directory uses .claude/<type>."""
        commands_dir = get_claude_artifacts_dir("commands", scope="project")
        assert commands_dir.name == "commands"
        assert commands_dir.parent.name == ".claude"


class TestCmdUpdate:
    """Tests for cmd_update CLI command."""

    def _make_installed_skill(self, target: Path, name: str, meta: dict | None = None) -> Path:
        """Create a fake installed skill directory with optional .agentkit.json metadata."""
        skill_dir = target / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\nContent"
        )
        if meta is not None:
            (skill_dir / ".agentkit.json").write_text(json.dumps(meta))
        return skill_dir

    def _base_args(self, target: str, **overrides) -> argparse.Namespace:
        defaults = dict(
            items=None, all=True, scope="global", project=False,
            target=target, backup=False, dry_run=False,
            no_cache=False, type="skills",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    _SOURCE_META = {
        "source_url": "https://github.com/org/repo/tree/main/skills",
        "clone_url": "https://github.com/org/repo.git",
        "branch": "main",
        "commit": "abc1234",
        "installed_at": "2024-01-01T00:00:00",
        "installed_by": "agentkit",
    }

    def _make_upstream(self, tmp: Path, *names: str) -> Path:
        """Build a fake upstream skills directory with the given skill names."""
        for name in names:
            d = tmp / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Updated\n---\nNew content"
            )
        return tmp

    def test_update_skips_artifacts_without_metadata(self, capsys):
        """Artifacts installed without .agentkit.json are warned and skipped."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._make_installed_skill(target, "no-meta-skill")  # no meta

            result = cmd_update(self._base_args(str(target)))

        assert result == 0
        assert "no-meta-skill" in capsys.readouterr().out

    def test_update_all_with_metadata(self, capsys):
        """Update all artifacts that have metadata re-installs from upstream."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as target_tmp, \
             tempfile.TemporaryDirectory() as upstream_tmp, \
             tempfile.TemporaryDirectory() as cache_tmp:

            target = Path(target_tmp)
            self._make_installed_skill(target, "my-skill", meta=self._SOURCE_META)
            upstream = self._make_upstream(Path(upstream_tmp), "my-skill")

            with patch("agentkit.cli.get_repo_cache_dir", return_value=Path(cache_tmp)), \
                 patch("agentkit.cli.clone_repo", return_value=upstream), \
                 patch("agentkit.cli.get_git_commit_hash", return_value="newdead"):
                result = cmd_update(self._base_args(str(target)))

        assert result == 0
        assert "my-skill" in capsys.readouterr().out

    def test_update_filters_by_items(self, capsys):
        """--items restricts which installed artifacts are updated."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as target_tmp, \
             tempfile.TemporaryDirectory() as upstream_tmp, \
             tempfile.TemporaryDirectory() as cache_tmp:

            target = Path(target_tmp)
            self._make_installed_skill(target, "skill-a", meta=self._SOURCE_META)
            self._make_installed_skill(target, "skill-b", meta=self._SOURCE_META)
            upstream = self._make_upstream(Path(upstream_tmp), "skill-a", "skill-b")

            with patch("agentkit.cli.get_repo_cache_dir", return_value=Path(cache_tmp)), \
                 patch("agentkit.cli.clone_repo", return_value=upstream), \
                 patch("agentkit.cli.get_git_commit_hash", return_value="newdead"):
                result = cmd_update(self._base_args(str(target), items="skill-a", all=False))

        assert result == 0
        out = capsys.readouterr().out
        assert "skill-a" in out
        assert "skill-b" not in out

    def test_update_warns_when_not_found_upstream(self, capsys):
        """Artifacts missing from the upstream repo are warned and skipped."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as target_tmp, \
             tempfile.TemporaryDirectory() as upstream_tmp, \
             tempfile.TemporaryDirectory() as cache_tmp:

            target = Path(target_tmp)
            self._make_installed_skill(target, "removed-skill", meta=self._SOURCE_META)
            upstream = Path(upstream_tmp)  # empty — artifact not present upstream

            with patch("agentkit.cli.get_repo_cache_dir", return_value=Path(cache_tmp)), \
                 patch("agentkit.cli.clone_repo", return_value=upstream), \
                 patch("agentkit.cli.get_git_commit_hash", return_value="abc"):
                result = cmd_update(self._base_args(str(target)))

        assert result == 0
        assert "removed-skill" in capsys.readouterr().out

    def test_update_dry_run_does_not_modify_files(self):
        """--dry-run shows a preview without changing installed artifact content."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as target_tmp, \
             tempfile.TemporaryDirectory() as upstream_tmp, \
             tempfile.TemporaryDirectory() as cache_tmp:

            target = Path(target_tmp)
            self._make_installed_skill(target, "dry-skill", meta=self._SOURCE_META)
            upstream = self._make_upstream(Path(upstream_tmp), "dry-skill")

            original = (target / "dry-skill" / "SKILL.md").read_text()

            with patch("agentkit.cli.get_repo_cache_dir", return_value=Path(cache_tmp)), \
                 patch("agentkit.cli.clone_repo", return_value=upstream), \
                 patch("agentkit.cli.get_git_commit_hash", return_value="newdead"):
                result = cmd_update(self._base_args(str(target), dry_run=True))

            assert result == 0
            assert (target / "dry-skill" / "SKILL.md").read_text() == original

    def test_update_no_items_selected_returns_zero(self, capsys):
        """Passing --items that matches no installed artifact returns an error."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self._make_installed_skill(target, "existing-skill", meta=self._SOURCE_META)

            result = cmd_update(self._base_args(str(target), items="nonexistent", all=False))

        assert result == 1

    def test_update_empty_install_dir_returns_zero(self, capsys):
        """An install directory with no artifacts exits cleanly."""
        from agentkit.cli import cmd_update

        with tempfile.TemporaryDirectory() as tmp:
            result = cmd_update(self._base_args(tmp))

        assert result == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])


class TestCmdDoctorOrphans:
    """doctor must accept every marker layout that discovery accepts."""

    def test_only_folders_without_any_marker_are_orphaned(self, tmp_path, capsys):
        from agentkit.cli import cmd_doctor

        global_dir = tmp_path / "global"
        layouts = {
            "keep-primary-marker": "PLUGIN.md",
            "keep-root-plugin-json": "plugin.json",
            "keep-nested-plugin-json": ".claude-plugin/plugin.json",
            "keep-qwen-extension-json": "qwen-extension.json",
            "ShouldBeOrphaned": "README.md",
        }
        for folder, marker in layouts.items():
            path = global_dir / folder / marker
            path.parent.mkdir(parents=True, exist_ok=True)
            content = '{"name": "x", "description": "d"}' if marker.endswith(".json") else "---\nname: x\ndescription: d\n---\nbody"
            path.write_text(content)

        dirs = {"personal": global_dir, "project": tmp_path / "missing-project"}
        args = argparse.Namespace(type="plugins", agent=None, check_updates=False)
        with patch("agentkit.cli.get_artifacts_dir", side_effect=lambda artifact_type, scope, agent: dirs[scope]):
            result = cmd_doctor(args)

        orphan_lines = [line for line in capsys.readouterr().out.splitlines() if "Orphaned directory" in line]
        assert result == 1
        assert len(orphan_lines) == 1
        assert "ShouldBeOrphaned" in orphan_lines[0]


class TestPublicApi:

    def test_readme_python_api_imports(self):
        """Every name imported in the README's Python API example is exported."""
        import re
        import agentkit

        readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
        block = re.search(r"from agentkit import \((.*?)\)", readme, re.S).group(1)
        names = [n.strip() for n in block.split(",") if n.strip()]
        assert names
        missing = [n for n in names if not hasattr(agentkit, n)]
        assert missing == []


class TestValidateMarkerLayouts:
    """validate_artifact_md must accept every layout discovery accepts."""

    @pytest.mark.parametrize("marker", ["plugin.json", ".claude-plugin/plugin.json", "qwen-extension.json"])
    def test_json_manifest_plugins_are_valid(self, tmp_path, marker):
        manifest = tmp_path / "p" / marker
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "p", "version": "1.0.0"}')

        assert validate_artifact_md(tmp_path / "p", artifact_type="plugins") == []

    def test_json_manifest_without_name_is_reported(self, tmp_path):
        (tmp_path / "plugin.json").write_text('{"description": "NameIsMissing"}')

        assert validate_artifact_md(tmp_path, artifact_type="plugins") == ["Missing required field: name"]

    def test_invalid_json_manifest_is_reported(self, tmp_path):
        (tmp_path / "qwen-extension.json").write_text("{not json")

        issues = validate_artifact_md(tmp_path, artifact_type="plugins")
        assert len(issues) == 1 and issues[0].startswith("Invalid JSON in qwen-extension.json")

    def test_flat_agent_file_is_validated_as_its_own_marker(self, tmp_path):
        valid = tmp_path / "valid.agent.md"
        valid.write_text("---\nname: valid\ndescription: d\n---\nPrompt")
        no_desc = tmp_path / "no-desc.agent.md"
        no_desc.write_text("---\nname: no-desc\n---\nPrompt")

        assert validate_artifact_md(valid, artifact_type="agents") == []
        assert validate_artifact_md(no_desc, artifact_type="agents") == ["Missing required field: description"]

    def test_cmd_validate_path_accepts_flat_file_and_nested_marker(self, tmp_path, capsys):
        from agentkit.cli import cmd_validate

        agent = tmp_path / "flat-agent.agent.md"
        agent.write_text("---\nname: flat-agent\ndescription: d\n---\nPrompt")
        nested = tmp_path / "nested-plugin" / ".claude-plugin" / "plugin.json"
        nested.parent.mkdir(parents=True)
        nested.write_text('{"name": "nested-plugin"}')

        base = dict(repo=None, branch=None, project=False, agent=None)
        assert cmd_validate(argparse.Namespace(path=str(agent), type="agents", **base)) == 0
        assert cmd_validate(argparse.Namespace(path=str(nested), type="plugins", **base)) == 0
        out = capsys.readouterr().out
        assert "flat-agent.agent.md" in out and "nested-plugin" in out


class TestCmdDoctorManagedDirs:

    def _run(self, tmp_path, agent: str, capsys) -> str:
        from agentkit.cli import cmd_doctor

        global_dir = tmp_path / "global"
        for folder in ("cache", "marketplaces", "store", "synced", "ShouldBeOrphaned"):
            (global_dir / folder).mkdir(parents=True)
        dirs = {"personal": global_dir, "project": tmp_path / "missing-project"}
        args = argparse.Namespace(type="plugins", agent=agent, check_updates=False)
        with patch("agentkit.cli.get_artifacts_dir", side_effect=lambda artifact_type, scope, agent: dirs[scope]):
            cmd_doctor(args)
        return capsys.readouterr().out

    def test_claude_managed_plugin_folders_are_skipped(self, tmp_path, capsys):
        out = self._run(tmp_path, "claude", capsys)

        orphan_lines = [line for line in out.splitlines() if "Orphaned directory" in line]
        assert len(orphan_lines) == 1 and "ShouldBeOrphaned" in orphan_lines[0]
        assert "Skipped Claude Code-managed folders: cache, marketplaces, store, synced" in out

    def test_managed_folders_are_agent_specific(self, tmp_path, capsys):
        out = self._run(tmp_path, "qwen", capsys)

        orphan_lines = [line for line in out.splitlines() if "Orphaned directory" in line]
        assert len(orphan_lines) == 5  # Claude's managed names mean nothing under ~/.qwen/extensions
        assert "managed folders" not in out
