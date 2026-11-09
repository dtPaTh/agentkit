"""
AgentKit Convention Conversion

Converts an artifact between agent conventions (e.g. Claude Code <-> Qwen Code)
using the per-agent tables in core.AGENT_CONFIG. A conversion direction simply
selects which side of the table replaces which, so every rule works both ways.

Flow for one artifact:
    1. Detect which agents' conventions the artifact uses (signals).
       - none                        -> nothing to convert
       - only the target's           -> nothing to convert
       - only another agent's        -> convert from that agent to the target
       - several agents' (mixed)     -> leave untouched, report a warning
    2. Token conversion (renames, path/context-file rewrites, semantic rules)
    3. Report-only checks: TOML commands the target can't read, and scripts
       invoking another agent's CLI (scripts are never changed for this)
    4. Structural step for plugins: ensure the target's manifest exists

Everything is computed before anything is written, so dry_run=True yields the
same report without touching the file system.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .core import (
    AGENT_CONFIG,
    METADATA_FILE,
    ARTIFACT_CONFIG,
    _find_marker_in_folder,
    get_agent_config,
    get_artifact_config,
    parse_artifact_md,
)

# Files larger than this are never rewritten
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024

# Files checked for invocations of other agents' CLIs (reported, never modified)
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts"}

# Report entry kinds, in display order
CHANGE_KINDS = ("rename", "rewrite", "convert", "create", "warning")

_KIND_LABELS = {
    "rename": ("rename", "renames"),
    "rewrite": ("file rewrite", "file rewrites"),
    "convert": ("conversion", "conversions"),
    "create": ("created file", "created files"),
    "warning": ("warning", "warnings"),
}


@dataclass(frozen=True)
class Change:
    """One entry of a conversion report."""
    kind: str    # one of CHANGE_KINDS
    path: str    # path relative to the artifact root (posix), "." for the artifact itself
    detail: str


def summarize_changes(changes: list[Change]) -> str:
    """Return a one-line summary like '2 renames, 1 file rewrite, 1 warning'."""
    counts = Counter(c.kind for c in changes)
    parts = []
    for kind in CHANGE_KINDS:
        n = counts.get(kind, 0)
        if n:
            singular, plural = _KIND_LABELS[kind]
            parts.append(f"{n} {singular if n == 1 else plural}")
    return ", ".join(parts) if parts else "no changes"


# =============================================================================
# Pattern helpers
# =============================================================================

# A token like ".claude" or "CLAUDE.md" must not be glued to other name characters:
# ".claude-plugin", ".claude.json", "MY_CLAUDE.md" stay untouched, while a trailing
# sentence period ("see CLAUDE.md.") is allowed.
_TOKEN_BEFORE = r"(?<![\w.-])"
_TOKEN_AFTER = r"(?![\w-])(?!\.\w)"


def _token_re(token: str) -> re.Pattern:
    return re.compile(_TOKEN_BEFORE + re.escape(token) + _TOKEN_AFTER)


def _type_dir_res(src: dict, tgt: dict) -> list[tuple[re.Pattern, str]]:
    """Patterns for '<src_dir>/<src_subdir>' where the type subdir differs (plugins -> extensions)."""
    result = []
    for artifact_type in ARTIFACT_CONFIG:
        src_sub = src["type_dirs"].get(artifact_type, artifact_type)
        tgt_sub = tgt["type_dirs"].get(artifact_type, artifact_type)
        if src_sub != tgt_sub:
            pattern = re.compile(
                _TOKEN_BEFORE + re.escape(src["config_dir"]) + r"([/\\])" + re.escape(src_sub) + r"(?![\w-])"
            )
            result.append((pattern, tgt_sub))
    return result


def _rewrite_config_dirs(text: str, src: dict, tgt: dict) -> tuple[str, int]:
    total = 0
    for pattern, tgt_sub in _type_dir_res(src, tgt):
        text, n = pattern.subn(lambda m: f"{tgt['config_dir']}{m.group(1)}{tgt_sub}", text)
        total += n
    text, n = _token_re(src["config_dir"]).subn(lambda _m: tgt["config_dir"], text)
    return text, total + n


def _cli_call_re(cli: str) -> re.Pattern:
    """Match an agent CLI invocation: argv item ("claude", / ['claude']) or shell call (claude -p)."""
    name = re.escape(cli)
    return re.compile(
        rf"""["']{name}["']\s*[,\])]"""            # subprocess argv / shutil.which("claude")
        rf"""|(?<![\w./-]){name}(?=[ \t]+--?[A-Za-z])"""  # shell: claude -p, claude --print
    )


def _cli_call_warnings(texts: dict[Path, str], rels: dict[Path, Path], target: str) -> list[Change]:
    """Warn about scripts invoking any agent CLI other than the target's. Scripts are not changed."""
    tgt = AGENT_CONFIG[target]
    changes = []
    for f, text in texts.items():
        if rels[f].suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        for name, cfg in AGENT_CONFIG.items():
            if name == target:
                continue
            matches = list(_cli_call_re(cfg["cli"]).finditer(text))
            if matches:
                line = text.count("\n", 0, matches[0].start()) + 1
                changes.append(Change(
                    "warning", rels[f].as_posix(),
                    f"script calls the `{cfg['cli']}` CLI ({len(matches)}×, first at line {line}) — "
                    f"left as is; port it to `{tgt['cli']}` if needed",
                ))
    return changes


def _agent_only_tools(agent: str) -> set[str]:
    """Tool names used by this agent that no other agent uses."""
    own = set(AGENT_CONFIG[agent]["tools"].values()) | set(AGENT_CONFIG[agent]["dropped_tools"])
    others: set[str] = set()
    for name, cfg in AGENT_CONFIG.items():
        if name != agent:
            others |= set(cfg["tools"].values()) | set(cfg["dropped_tools"])
    return own - others


# =============================================================================
# Frontmatter helpers (line-based, preserving everything we don't touch)
# =============================================================================

_FM_RE = re.compile(r"\A(﻿?---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|\Z))", re.S)


def _split_frontmatter(text: str) -> Optional[tuple[str, list[str], str, str, str]]:
    """Return (head, lines, newline, close, body) or None when there is no frontmatter."""
    m = _FM_RE.match(text)
    if not m:
        return None
    fm = m.group(2)
    nl = "\r\n" if "\r\n" in fm or m.group(1).endswith("\r\n") else "\n"
    return m.group(1), fm.split(nl), nl, m.group(3), text[m.end():]


def _join_frontmatter(head: str, lines: list[str], nl: str, close: str, body: str) -> str:
    return head + nl.join(lines) + close + body


def _find_key(lines: list[str], key: str) -> Optional[tuple[int, int, str]]:
    """Find a top-level key; return (start, end_exclusive, inline_value)."""
    pattern = re.compile(r"^" + re.escape(key) + r"[ \t]*:[ \t]*(.*?)[ \t]*$")
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and (lines[j][0] in " \t" or lines[j].lstrip().startswith("- ")):
            j += 1
        return i, j, m.group(1)
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_tool_string(value: str) -> list[str]:
    """Split 'Read, Bash(git add:*)' or 'Read Grep' at top-level separators."""
    has_top_comma = False
    depth = 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            has_top_comma = True
            break

    items, current, depth = [], [], 0
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        is_sep = depth == 0 and (ch == "," if has_top_comma else ch.isspace())
        if is_sep:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return [_unquote(i) for i in items if i.strip()]


def _parse_tools(lines: list[str], start: int, end: int, inline: str) -> Optional[tuple[list[str], str]]:
    """Return (tools, style) with style in list/flow/string, or None if unsupported."""
    if inline == "":
        items = []
        for line in lines[start + 1:end]:
            m = re.match(r"^\s*-\s*(.*?)\s*$", line)
            if not m:
                return None
            items.append(_unquote(m.group(1)))
        return [i for i in items if i], "list"
    if end > start + 1:
        return None  # inline value plus continuation lines (e.g. block scalar) — unsupported
    if inline.startswith("[") and inline.endswith("]"):
        return _split_tool_string(inline[1:-1]), "flow"
    return _split_tool_string(_unquote(inline)), "string"


_PLAIN_NAME = re.compile(r"[A-Za-z_][\w.\-]*")


def _yaml_scalar(value: str, flow: bool = False) -> str:
    if _PLAIN_NAME.fullmatch(value):
        return value
    if not flow and ": " not in value and " #" not in value and value[:1] not in "\"'{[&*!|>%@`#":
        return value
    return json.dumps(value)


def _format_tools(key: str, tools: list[str], style: str) -> list[str]:
    if style == "list":
        if not tools:
            return [f"{key}: []"]
        return [f"{key}:"] + [f"  - {_yaml_scalar(t)}" for t in tools]
    if style == "flow":
        return [f"{key}: [{', '.join(_yaml_scalar(t, flow=True) for t in tools)}]"]
    return [f"{key}: {_yaml_scalar(', '.join(tools))}"]


def _map_tools(tools: list[str], src: dict, tgt: dict):
    """Map tool names src -> tgt. Returns (new_tools, mapped, dropped, patterned)."""
    src_to_canon = {v: k for k, v in src["tools"].items()}
    out, mapped, dropped, patterned = [], [], [], []
    for tool in tools:
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\((.*)\)", tool)
        if m:
            if m.group(1) in src_to_canon or m.group(1) in src["dropped_tools"]:
                patterned.append(tool)
            out.append(tool)
        elif tool in src_to_canon:
            new = tgt["tools"][src_to_canon[tool]]
            out.append(new)
            mapped.append(f"{tool}→{new}")
        elif tool in src["dropped_tools"]:
            dropped.append(tool)
        else:
            out.append(tool)
    return out, mapped, dropped, patterned


def _convert_tools_key(
    lines: list[str], key: str, new_key: str, new_style: Optional[str],
    src: dict, tgt: dict, rel: str,
) -> tuple[list[str], list[Change]]:
    """Convert one tools key in frontmatter lines. new_style=None keeps the original style."""
    found = _find_key(lines, key)
    if not found:
        return lines, []
    start, end, inline = found
    parsed = _parse_tools(lines, start, end, inline)
    if parsed is None:
        return lines, [Change("warning", rel, f"'{key}' has an unsupported format — not converted")]
    tools, style = parsed
    new_tools, mapped, dropped, patterned = _map_tools(tools, src, tgt)
    target_style = new_style or style

    changes = []
    details = []
    if new_key != key:
        details.append(f"{key} → {new_key}")
    if target_style != style:
        details.append(f"{style} → {target_style} format")
    if mapped:
        details.append("tools " + ", ".join(mapped))
    if dropped:
        details.append("dropped " + ", ".join(dropped) + f" (no {tgt['label']} equivalent)")
    if details:
        lines = lines[:start] + _format_tools(new_key, new_tools, target_style) + lines[end:]
        changes.append(Change("convert", rel, "; ".join(details)))
    if patterned:
        changes.append(Change(
            "warning", rel,
            f"'{key}' patterned entries kept unchanged, verify for {tgt['label']}: " + ", ".join(patterned),
        ))
    return lines, changes


# =============================================================================
# Per-role semantic rules
# =============================================================================

def _syntax_warnings(text: str, syntax: list[tuple[str, str]], tgt: dict, rel: str) -> list[Change]:
    return [
        Change("warning", rel, f"{desc} has no {tgt['label']} equivalent — review manually")
        for pattern, desc in syntax
        if re.search(pattern, text)
    ]


def _convert_command(text: str, src: dict, tgt: dict, rel: str) -> tuple[str, list[Change]]:
    changes = []
    count = text.count(src["args_placeholder"])
    if count:
        text = text.replace(src["args_placeholder"], tgt["args_placeholder"])
        changes.append(Change("convert", rel, f"{src['args_placeholder']} → {tgt['args_placeholder']} ({count}×)"))
    changes += _syntax_warnings(text, src["command_syntax"], tgt, rel)
    return text, changes


def _convert_skill(text: str, src: dict, tgt: dict, rel: str) -> tuple[str, list[Change]]:
    changes = []
    split = _split_frontmatter(text)
    if split:
        head, lines, nl, close, body = split
        lines, key_changes = _convert_tools_key(
            lines, src["skill_tools_key"], tgt["skill_tools_key"], tgt["skill_tools_style"], src, tgt, rel,
        )
        changes += key_changes
        text = _join_frontmatter(head, lines, nl, close, body)
    changes += _syntax_warnings(text, src["skill_syntax"], tgt, rel)
    return text, changes


def _convert_agent(text: str, src: dict, tgt: dict, rel: str) -> tuple[str, list[Change]]:
    split = _split_frontmatter(text)
    if not split:
        return text, []
    head, lines, nl, close, body = split
    changes = []
    for key in ("tools", "disallowedTools"):
        lines, key_changes = _convert_tools_key(lines, key, key, None, src, tgt, rel)
        changes += key_changes

    found = _find_key(lines, src["agent_mode_key"])
    if found:
        start, _end, inline = found
        value = _unquote(inline)
        canon = {v: k for k, v in src["agent_modes"].items()}.get(value)
        if canon:
            new_value = tgt["agent_modes"][canon]
            lines[start] = f"{tgt['agent_mode_key']}: {new_value}"
            changes.append(Change(
                "convert", rel, f"{src['agent_mode_key']}: {value} → {tgt['agent_mode_key']}: {new_value}",
            ))
        else:
            changes.append(Change(
                "warning", rel,
                f"{src['agent_mode_key']}: {value} has no {tgt['label']} equivalent — kept unchanged",
            ))
    return _join_frontmatter(head, lines, nl, close, body), changes


_ROLE_CONVERTERS = {
    "command": _convert_command,
    "skill": _convert_skill,
    "agent": _convert_agent,
}


def _file_role(rel: Path, artifact_type: str, is_flat: bool) -> Optional[str]:
    """Which semantic rule set applies to a file: command / skill / agent / None."""
    if artifact_type == "skills":
        return "skill" if rel.name == "SKILL.md" else None
    if artifact_type == "agents":
        return "agent" if (is_flat or rel.name == "AGENT.md") else None
    if artifact_type == "commands":
        return "command" if rel.suffix == ".md" else None
    if artifact_type == "plugins" and len(rel.parts) > 1:
        top = rel.parts[0]
        if top == "commands" and rel.suffix == ".md":
            return "command"
        if top == "agents" and rel.suffix == ".md":
            return "agent"
        if top == "skills" and rel.name == "SKILL.md":
            return "skill"
    return None


def _is_command_file(rel: Path, artifact_type: str) -> bool:
    return artifact_type == "commands" or (artifact_type == "plugins" and rel.parts[:1] == ("commands",))


# =============================================================================
# Detection
# =============================================================================

def _detect_signals(agent: str, texts: dict[Path, str], rels: dict[Path, Path], roles: dict) -> list[str]:
    """List the conventions of `agent` found in the artifact."""
    cfg = AGENT_CONFIG[agent]
    signals = []

    rel_list = list(rels.values())
    if any(r.name.casefold() == cfg["context_file"].casefold() for r in rel_list):
        signals.append(f"{cfg['context_file']} file")
    if any(cfg["config_dir"] in r.parts[:-1] for r in rel_list):
        signals.append(f"{cfg['config_dir']}/ folder")

    dir_re = _token_re(cfg["config_dir"])
    ctx_re = _token_re(cfg["context_file"])
    agent_tools = _agent_only_tools(agent)
    found_dir_ref = found_ctx_ref = found_ph = found_key = found_tools = found_mode = False

    for f, text in texts.items():
        found_dir_ref = found_dir_ref or bool(dir_re.search(text))
        found_ctx_ref = found_ctx_ref or bool(ctx_re.search(text))
        role = roles.get(f)
        if role == "command":
            found_ph = found_ph or cfg["args_placeholder"] in text
        split = _split_frontmatter(text) if role in ("skill", "agent") else None
        if split and role == "skill":
            found_key = found_key or _find_key(split[1], cfg["skill_tools_key"]) is not None
        if split and role == "agent":
            found_mode = found_mode or _find_key(split[1], cfg["agent_mode_key"]) is not None
            for key in ("tools", "disallowedTools"):
                found = _find_key(split[1], key)
                parsed = _parse_tools(split[1], *found) if found else None
                if parsed and agent_tools & set(parsed[0]):
                    found_tools = True

    if found_dir_ref:
        signals.append(f"{cfg['config_dir']}/ paths")
    if found_ctx_ref:
        signals.append(f"{cfg['context_file']} references")
    if found_ph:
        signals.append(cfg["args_placeholder"])
    if found_key:
        signals.append(f"'{cfg['skill_tools_key']}'")
    if found_tools:
        signals.append(f"{cfg['label']} tool names")
    if found_mode:
        signals.append(f"'{cfg['agent_mode_key']}'")
    return signals


# =============================================================================
# File helpers
# =============================================================================

def _artifact_files(path: Path) -> tuple[Path, list[Path]]:
    """Return (root, files) for a folder or flat-file artifact."""
    if path.is_file():
        return path.parent, [path]
    files = [
        p for p in path.rglob("*")
        if p.is_file() and p.name != METADATA_FILE and ".git" not in p.relative_to(path).parts
    ]
    return path, sorted(files)


def _read_text(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# =============================================================================
# Conversion steps
# =============================================================================

def _convert_tokens(
    root: Path, texts: dict[Path, str], rels: dict[Path, Path], roles: dict,
    src: dict, tgt: dict, is_flat: bool, dry_run: bool,
) -> list[Change]:
    changes: list[Change] = []
    new_texts: dict[Path, str] = {}
    ctx_re = _token_re(src["context_file"])

    # 1. Content rewrites + semantic rules (computed in memory)
    for f, text in texts.items():
        rel = rels[f].as_posix()
        new, n_dir = _rewrite_config_dirs(text, src, tgt)
        new, n_ctx = ctx_re.subn(lambda _m: tgt["context_file"], new)
        details = []
        if n_dir:
            details.append(f"{src['config_dir']} → {tgt['config_dir']} ({n_dir}×)")
        if n_ctx:
            details.append(f"{src['context_file']} → {tgt['context_file']} ({n_ctx}×)")
        if details:
            changes.append(Change("rewrite", rel, ", ".join(details)))

        converter = _ROLE_CONVERTERS.get(roles.get(f))
        if converter:
            new, role_changes = converter(new, src, tgt, rel)
            changes += role_changes
        if new != text:
            new_texts[f] = new

    # 2. Renames (files first, then folders deepest-first, so paths stay valid)
    renames: list[tuple[Path, Path]] = []
    if not is_flat:
        for f, rel in rels.items():
            if rel.name.casefold() == src["context_file"].casefold():
                renames.append((f, f.with_name(tgt["context_file"])))
        dirs = sorted(
            (p for p in root.rglob(src["config_dir"]) if p.is_dir()),
            key=lambda p: len(p.parts), reverse=True,
        )
        renames += [(d, d.with_name(tgt["config_dir"])) for d in dirs]

    applied_renames = []
    for old, new in renames:
        rel_old = old.relative_to(root).as_posix()
        rel_new = new.relative_to(root).as_posix()
        if new.exists():
            changes.append(Change("warning", rel_old, f"not renamed — {rel_new} already exists"))
            continue
        suffix = "/" if old.is_dir() else ""
        changes.append(Change("rename", rel_old + suffix, f"→ {rel_new}{suffix}"))
        applied_renames.append((old, new))

    if not dry_run:
        for f, new in new_texts.items():
            f.write_bytes(new.encode("utf-8"))
        for old, new in applied_renames:
            old.rename(new)
    return changes


def _ensure_plugin_manifest(path: Path, tgt: dict, dry_run: bool) -> list[Change]:
    """Create the target agent's plugin manifest if the plugin has none."""
    if any((path / marker).exists() for marker in tgt["plugin_markers"]):
        return []

    marker = _find_marker_in_folder(path, get_artifact_config("plugins"))
    info = parse_artifact_md(marker, artifact_type="plugins") if marker else {}
    manifest = {"name": info.get("name") or path.name}
    for field in ("version", "description"):
        if info.get(field):
            manifest[field] = info[field]

    dest = path / tgt["plugin_manifest"]
    source_rel = marker.relative_to(path).as_posix() if marker else "folder name"
    changes = [Change("create", tgt["plugin_manifest"], f"generated from {source_rel} (name, version, description)")]

    unconverted = []
    if (path / "hooks").is_dir():
        unconverted.append("hooks/")
    if (path / ".mcp.json").is_file():
        unconverted.append(".mcp.json")
    if marker and marker.suffix == ".json":
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        unconverted += [f"'{k}' in {source_rel}" for k in ("hooks", "mcpServers", "settings", "channels") if k in data]
    if unconverted:
        hint = f" — {tgt['plugin_hint']}" if tgt.get("plugin_hint") else ""
        changes.append(Change("warning", ".", f"not converted: {', '.join(unconverted)}{hint}"))

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return changes


# =============================================================================
# Public API
# =============================================================================

def convert_artifact(
    artifact_path: Path,
    artifact_type: str,
    target_agent: str,
    dry_run: bool = False,
) -> list[Change]:
    """
    Convert an artifact (folder or flat file) in place to the target agent's conventions.

    Args:
        artifact_path: installed artifact path (or the source path when dry_run=True)
        artifact_type: skills, commands, agents or plugins
        target_agent: key of core.AGENT_CONFIG, e.g. "claude" or "qwen"
        dry_run: compute the report without writing anything

    Returns:
        List of Change entries (empty when nothing needed converting)
    """
    path = Path(artifact_path)
    get_artifact_config(artifact_type)
    target = target_agent.strip().lower()
    tgt = get_agent_config(target)
    is_flat = path.is_file()

    root, files = _artifact_files(path)
    rels = {f: f.relative_to(root) for f in files}
    texts = {f: t for f in files if (t := _read_text(f)) is not None}
    roles = {f: _file_role(rels[f], artifact_type, is_flat) for f in files}

    changes: list[Change] = []

    # 1. Detection
    target_signals = _detect_signals(target, texts, rels, roles)
    sources = [
        (name, signals)
        for name in AGENT_CONFIG
        if name != target and (signals := _detect_signals(name, texts, rels, roles))
    ]
    if sources and (target_signals or len(sources) > 1):
        found = [(target, target_signals)] + sources if target_signals else sources
        desc = "; ".join(f"{AGENT_CONFIG[n]['label']}: {', '.join(s)}" for n, s in found)
        changes.append(Change("warning", ".", f"mixed agent conventions ({desc}) — content left unchanged"))
    elif sources:
        src = AGENT_CONFIG[sources[0][0]]
        changes += _convert_tokens(root, texts, rels, roles, src, tgt, is_flat, dry_run)

    # 2. Command formats the target cannot read
    for rel in rels.values():
        if rel.suffix == ".toml" and _is_command_file(rel, artifact_type) and ".toml" not in tgt["command_formats"]:
            changes.append(Change(
                "warning", rel.as_posix(), f"TOML command not converted — {tgt['label']} reads Markdown commands only",
            ))

    # 3. Scripts invoking other agents' CLIs (independent of detection; report only)
    changes += _cli_call_warnings(texts, rels, target)

    # 4. Structural: plugin manifest for the target
    if artifact_type == "plugins" and not is_flat:
        changes += _ensure_plugin_manifest(path, tgt, dry_run)

    return changes
