"""Tests for agent targeting and Claude Code <-> Qwen Code convention conversion."""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agentkit import convert_artifact, get_artifacts_dir, get_claude_artifacts_dir, summarize_changes


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def kinds(changes) -> list[str]:
    return [c.kind for c in changes]


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# =============================================================================
# Directory resolution
# =============================================================================

class TestAgentDirs:

    def test_claude_dirs(self):
        assert get_artifacts_dir("skills", "personal", "claude") == Path.home() / ".claude" / "skills"
        assert get_artifacts_dir("plugins", "personal", "claude") == Path.home() / ".claude" / "plugins"

    def test_qwen_dirs_map_plugins_to_extensions(self):
        assert get_artifacts_dir("skills", "personal", "qwen") == Path.home() / ".qwen" / "skills"
        assert get_artifacts_dir("plugins", "personal", "qwen") == Path.home() / ".qwen" / "extensions"

    def test_project_scope_uses_cwd(self):
        assert get_artifacts_dir("agents", "project", "qwen") == Path.cwd() / ".qwen" / "agents"

    def test_default_agent_is_used_when_none_given(self):
        with patch("agentkit.core.DEFAULT_AGENT", "qwen"):
            assert get_artifacts_dir("commands", "personal") == Path.home() / ".qwen" / "commands"

    def test_legacy_claude_wrapper_ignores_default_agent(self):
        with patch("agentkit.core.DEFAULT_AGENT", "qwen"):
            assert get_claude_artifacts_dir("skills") == Path.home() / ".claude" / "skills"

    def test_unsupported_agent_raises(self):
        with pytest.raises(ValueError, match="Unsupported agent"):
            get_artifacts_dir("skills", "personal", "copilot")


# =============================================================================
# Claude -> Qwen
# =============================================================================

class TestClaudeToQwen:

    def test_skill_paths_context_file_and_allowed_tools(self, tmp_path):
        skill = tmp_path / "my-skill"
        write(skill / "SKILL.md", (
            "---\n"
            "name: my-skill\n"
            "description: Test\n"
            "allowed-tools: Read, Bash, Grep, Bash(git status)\n"
            "---\n"
            "Read ~/.claude/skills/my-skill/ref.md and the project CLAUDE.md.\n"
            "Untouched: .claude-plugin/plugin.json, ~/.claude.json, MY_CLAUDE.md\n"
        ))
        write(skill / "CLAUDE.md", "context ShouldBeRenamed\n")
        write(skill / ".claude" / "settings.json", "{}\n")

        changes = convert_artifact(skill, "skills", "qwen")

        assert read(skill / "SKILL.md") == (
            "---\n"
            "name: my-skill\n"
            "description: Test\n"
            "allowedTools:\n"
            "  - ReadFile\n"
            "  - Shell\n"
            "  - Grep\n"
            "  - Bash(git status)\n"
            "---\n"
            "Read ~/.qwen/skills/my-skill/ref.md and the project QWEN.md.\n"
            "Untouched: .claude-plugin/plugin.json, ~/.claude.json, MY_CLAUDE.md\n"
        )
        assert read(skill / "QWEN.md") == "context ShouldBeRenamed\n"
        assert not (skill / "CLAUDE.md").exists()
        assert (skill / ".qwen" / "settings.json").is_file()
        assert not (skill / ".claude").exists()

        renames = sorted(c.path for c in changes if c.kind == "rename")
        assert renames == [".claude/", "CLAUDE.md"]
        warnings = [c.detail for c in changes if c.kind == "warning"]
        assert len(warnings) == 1 and "Bash(git status)" in warnings[0]

    def test_context_file_not_renamed_when_target_exists(self, tmp_path):
        skill = tmp_path / "s"
        write(skill / "SKILL.md", "---\nname: s\ndescription: d\n---\nbody\n")
        write(skill / "CLAUDE.md", "ShouldStayClaude\n")
        write(skill / "QWEN.md", "ShouldStayQwen\n")

        changes = convert_artifact(skill, "skills", "qwen")

        assert read(skill / "CLAUDE.md") == "ShouldStayClaude\n"
        assert read(skill / "QWEN.md") == "ShouldStayQwen\n"
        # Both files present = mixed conventions -> left unchanged with a warning
        assert kinds(changes) == ["warning"]
        assert "mixed agent conventions" in changes[0].detail

    def test_command_arguments_placeholder_and_positional_warning(self, tmp_path):
        cmd = tmp_path / "review"
        write(cmd / "COMMAND.md", "---\nname: review\ndescription: d\n---\nReview $ARGUMENTS, focus on $1.\n")

        changes = convert_artifact(cmd, "commands", "qwen")

        assert read(cmd / "COMMAND.md").endswith("Review {{args}}, focus on $1.\n")
        assert [c.detail for c in changes if c.kind == "convert"] == ["$ARGUMENTS → {{args}} (1×)"]
        assert any("positional argument" in c.detail for c in changes if c.kind == "warning")

    def test_agent_tools_and_permission_mode(self, tmp_path):
        agent = write(tmp_path / "reviewer.agent.md", (
            "---\n"
            "name: reviewer\n"
            "description: d\n"
            "tools: Read, Glob, Bash, BashOutput\n"
            "permissionMode: acceptEdits\n"
            "---\n"
            "You review code.\n"
        ))

        changes = convert_artifact(agent, "agents", "qwen")

        assert read(agent) == (
            "---\n"
            "name: reviewer\n"
            "description: d\n"
            "tools: ReadFile, Glob, Shell\n"
            "approvalMode: auto-edit\n"
            "---\n"
            "You review code.\n"
        )
        details = " | ".join(c.detail for c in changes)
        assert "Read→ReadFile" in details and "Bash→Shell" in details and "dropped BashOutput" in details

    def test_plugin_gets_qwen_manifest_and_nested_rules(self, tmp_path):
        plugin = tmp_path / "my-plugin"
        write(plugin / ".claude-plugin" / "plugin.json",
              json.dumps({"name": "my-plugin", "version": "1.2.3", "description": "PluginDesc", "mcpServers": {}}))
        write(plugin / "commands" / "go.md", "---\ndescription: d\n---\nGo $ARGUMENTS\n")
        write(plugin / "hooks" / "hooks.json", "{}")
        write(plugin / "README.md", "Installs into ~/.claude/plugins/my-plugin\n")

        changes = convert_artifact(plugin, "plugins", "qwen")

        manifest = json.loads(read(plugin / "qwen-extension.json"))
        assert manifest == {"name": "my-plugin", "version": "1.2.3", "description": "PluginDesc"}
        assert read(plugin / "commands" / "go.md").endswith("Go {{args}}\n")
        assert read(plugin / "README.md") == "Installs into ~/.qwen/extensions/my-plugin\n"
        assert (plugin / ".claude-plugin" / "plugin.json").is_file()  # kept: Qwen reads it natively
        warning = next(c.detail for c in changes if c.kind == "warning")
        assert "hooks/" in warning and "'mcpServers'" in warning and "qwen extensions install" in warning


# =============================================================================
# Qwen -> Claude
# =============================================================================

class TestQwenToClaude:

    def test_skill_allowed_tools_list_to_string(self, tmp_path):
        skill = tmp_path / "s"
        write(skill / "SKILL.md", (
            "---\n"
            "name: s\n"
            "description: d\n"
            "allowedTools:\n"
            "  - ReadFile\n"
            "  - Shell\n"
            "---\n"
            "See QWEN.md and .qwen/settings.json\n"
        ))
        write(skill / "QWEN.md", "ShouldBecomeClaudeMd\n")

        convert_artifact(skill, "skills", "claude")

        assert read(skill / "SKILL.md") == (
            "---\n"
            "name: s\n"
            "description: d\n"
            "allowed-tools: Read, Bash\n"
            "---\n"
            "See CLAUDE.md and .claude/settings.json\n"
        )
        assert read(skill / "CLAUDE.md") == "ShouldBecomeClaudeMd\n"

    def test_command_args_and_injection_warning(self, tmp_path):
        cmd = tmp_path / "c"
        write(cmd / "COMMAND.md", "---\nname: c\ndescription: d\n---\nRun !{git diff} on {{args}}\n")
        write(cmd / "legacy.toml", 'prompt = "x"\n')

        changes = convert_artifact(cmd, "commands", "claude")

        assert read(cmd / "COMMAND.md").endswith("Run !{git diff} on $ARGUMENTS\n")
        warnings = [c.detail for c in changes if c.kind == "warning"]
        assert any("shell injection" in w for w in warnings)
        assert any(c.path == "legacy.toml" and "TOML" in c.detail for c in changes)

    def test_agent_approval_mode_and_list_tools(self, tmp_path):
        agent = write(tmp_path / "a.agent.md", (
            "---\nname: a\ndescription: d\ntools:\n  - ReadFile\n  - WriteFile\napprovalMode: yolo\n---\nbody\n"
        ))

        convert_artifact(agent, "agents", "claude")

        assert read(agent) == (
            "---\nname: a\ndescription: d\ntools:\n  - Read\n  - Write\npermissionMode: bypassPermissions\n---\nbody\n"
        )

    def test_unmapped_mode_is_warned_and_kept(self, tmp_path):
        agent = write(tmp_path / "a.agent.md", "---\nname: a\ndescription: d\napprovalMode: bubble\n---\nbody\n")

        changes = convert_artifact(agent, "agents", "claude")

        assert "approvalMode: bubble" in read(agent)
        assert any("bubble" in c.detail for c in changes if c.kind == "warning")

    def test_qwen_extension_gets_claude_manifest(self, tmp_path):
        ext = tmp_path / "ext"
        write(ext / "qwen-extension.json", json.dumps({"name": "ext", "version": "0.1.0", "settings": []}))

        changes = convert_artifact(ext, "plugins", "claude")

        assert json.loads(read(ext / ".claude-plugin" / "plugin.json")) == {"name": "ext", "version": "0.1.0"}
        assert any("'settings'" in c.detail for c in changes if c.kind == "warning")


# =============================================================================
# No-op, dry-run, CRLF
# =============================================================================

class TestNoOpAndDryRun:

    def _claude_skill(self, root: Path) -> Path:
        skill = root / "s"
        write(skill / "SKILL.md", "---\nname: s\ndescription: d\nallowed-tools: Read\n---\nUse ~/.claude/x\n")
        write(skill / "CLAUDE.md", "ctx\n")
        return skill

    def test_same_agent_is_byte_identical(self, tmp_path):
        skill = self._claude_skill(tmp_path)
        before = snapshot(skill)

        assert convert_artifact(skill, "skills", "claude") == []
        assert snapshot(skill) == before

    def test_neutral_artifact_is_untouched(self, tmp_path):
        skill = tmp_path / "s"
        write(skill / "SKILL.md", "---\nname: s\ndescription: d\n---\nPlain body\n")
        before = snapshot(skill)

        assert convert_artifact(skill, "skills", "qwen") == []
        assert snapshot(skill) == before

    def test_dry_run_reports_without_writing(self, tmp_path):
        skill = self._claude_skill(tmp_path)
        before = snapshot(skill)

        dry = convert_artifact(skill, "skills", "qwen", dry_run=True)
        assert snapshot(skill) == before

        real = convert_artifact(skill, "skills", "qwen")
        assert dry == real
        assert summarize_changes(real) == "1 rename, 1 file rewrite, 1 conversion"

    def test_crlf_line_endings_preserved(self, tmp_path):
        skill = tmp_path / "s"
        (skill).mkdir()
        (skill / "SKILL.md").write_bytes(b"---\r\nname: s\r\nallowed-tools: Read\r\n---\r\nSee CLAUDE.md\r\n")

        convert_artifact(skill, "skills", "qwen")

        assert (skill / "SKILL.md").read_bytes() == (
            b"---\r\nname: s\r\nallowedTools:\r\n  - ReadFile\r\n---\r\nSee QWEN.md\r\n"
        )


# =============================================================================
# CLI end-to-end
# =============================================================================

class TestCmdInstallAgent:

    def _args(self, target: Path, **overrides) -> argparse.Namespace:
        defaults = dict(
            repo_url=None, repo="https://github.com/org/repo", branch=None, items=None, skills=None,
            all=True, scope="global", project=False, target=str(target), force=False, backup=False,
            dry_run=False, type="skills", no_cache=False, agent="qwen",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _run(self, upstream: Path, args, cache: Path):
        from agentkit.cli import cmd_install
        repo_info = {"url": args.repo, "clone_url": args.repo + ".git", "branch": "main"}
        with patch("agentkit.cli.prepare_repo_info", return_value=repo_info), \
             patch("agentkit.cli.get_cloned_repo", return_value=upstream), \
             patch("agentkit.cli.get_repo_cache_dir", return_value=cache), \
             patch("agentkit.cli.get_git_commit_hash", return_value="abc1234"):
            return cmd_install(args)

    def _upstream(self, root: Path) -> Path:
        write(root / "skills" / "converted-skill" / "SKILL.md",
              "---\nname: converted-skill\ndescription: d\n---\nEdit .claude/settings.json\n")
        write(root / "skills" / "plain-skill" / "SKILL.md",
              "---\nname: plain-skill\ndescription: d\n---\nNothing agent specific\n")
        return root

    def test_install_converts_and_reports(self, tmp_path, capsys):
        upstream = self._upstream(tmp_path / "up")
        target = tmp_path / "target"

        assert self._run(upstream, self._args(target), tmp_path / "cache") == 0

        assert read(target / "converted-skill" / "SKILL.md").endswith("Edit .qwen/settings.json\n")
        assert read(target / "plain-skill" / "SKILL.md").endswith("Nothing agent specific\n")
        meta = json.loads(read(target / "converted-skill" / ".agentkit.json"))
        assert meta["commit"] == "abc1234"  # metadata untouched by conversion
        out = capsys.readouterr().out
        assert "converted for Qwen Code: 1 file rewrite" in out
        assert "Converted 1 skills to Qwen Code conventions" in out
        # Source cache must never be modified
        assert read(upstream / "skills" / "converted-skill" / "SKILL.md").endswith("Edit .claude/settings.json\n")

    def test_install_dry_run_reports_without_writing(self, tmp_path, capsys):
        upstream = self._upstream(tmp_path / "up")
        target = tmp_path / "target"

        assert self._run(upstream, self._args(target, dry_run=True), tmp_path / "cache") == 0

        assert not target.exists()
        assert read(upstream / "skills" / "converted-skill" / "SKILL.md").endswith("Edit .claude/settings.json\n")
        assert "would convert for Qwen Code" in capsys.readouterr().out

    def test_install_for_claude_does_not_convert(self, tmp_path, capsys):
        upstream = self._upstream(tmp_path / "up")
        target = tmp_path / "target"

        assert self._run(upstream, self._args(target, agent="claude"), tmp_path / "cache") == 0

        assert read(target / "converted-skill" / "SKILL.md").endswith("Edit .claude/settings.json\n")
        out = capsys.readouterr().out
        assert "↳" not in out and "conventions:" not in out


# =============================================================================
# Scripts calling agent CLIs (report only)
# =============================================================================

class TestScriptCliCalls:

    def _skill(self, root: Path) -> Path:
        skill = root / "s"
        write(skill / "SKILL.md", "---\nname: s\ndescription: d\n---\nRun `claude -p` via the script.\n")
        return skill

    def test_other_agent_cli_calls_are_warned_and_scripts_unchanged(self, tmp_path):
        skill = self._skill(tmp_path)
        argv_script = write(skill / "scripts" / "calls_claude_argv.py",
                            'import subprocess\n\ncmd = ["claude", "-p", prompt]\nshutil.which("claude")\n')
        shell_script = write(skill / "scripts" / "calls_claude_shell.sh", "#!/bin/sh\nclaude --print \"$1\"\n")
        lookalikes = write(skill / "scripts" / "no_cli_call.py",
                           'import os\nos.environ.pop("CLAUDECODE")\nname = "claude-api"\nmodel = "claude"\n')
        before = snapshot(skill / "scripts")

        changes = convert_artifact(skill, "skills", "qwen")

        assert snapshot(skill / "scripts") == before
        cli_warnings = {c.path: c.detail for c in changes if "CLI" in c.detail}
        assert set(cli_warnings) == {"scripts/calls_claude_argv.py", "scripts/calls_claude_shell.sh"}
        assert "`claude` CLI (2×, first at line 3)" in cli_warnings["scripts/calls_claude_argv.py"]
        assert "`claude` CLI (1×, first at line 2)" in cli_warnings["scripts/calls_claude_shell.sh"]
        assert "port it to `qwen`" in cli_warnings["scripts/calls_claude_argv.py"]
        assert lookalikes.name not in " ".join(cli_warnings)

    def test_target_agent_cli_is_not_warned(self, tmp_path):
        skill = self._skill(tmp_path)
        write(skill / "run.py", 'cmd = ["claude", "-p", x]\n')

        assert convert_artifact(skill, "skills", "claude") == []

    def test_reverse_direction_warns_about_qwen_cli(self, tmp_path):
        skill = self._skill(tmp_path)
        write(skill / "run.ps1", "qwen -p $prompt\n")

        changes = convert_artifact(skill, "skills", "claude")

        assert [(c.path, c.kind) for c in changes] == [("run.ps1", "warning")]
        assert "`qwen` CLI" in changes[0].detail and "port it to `claude`" in changes[0].detail
