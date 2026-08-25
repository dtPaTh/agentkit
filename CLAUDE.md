# AgentKit - Project Memory

## Project Overview
Cross-platform CLI tool for managing Claude Code and Claude Desktop skills. Written in Python with no external dependencies (uses only standard library).

## Tech Stack
- **Language**: Python 3.10+
- **Build System**: setuptools with pyproject.toml
- **Entry Point**: `agentkit` command via `agentkit:main`

## Project Structure
```
agentkit/
├── agentkit/
│   ├── __init__.py    # Package init, exports main()
│   ├── cli.py         # CLI argument parsing and commands
│   └── core.py        # Core logic (git operations, skill discovery, etc.)
├── tests/
├── pyproject.toml     # Project configuration
└── install.sh         # Installation script
```

## Key Commands
- `agentkit list [URL]` - List available artifacts from a repository
- `agentkit install [URL]` - Install artifacts from a repository
- `agentkit installed` - List locally installed artifacts
- `agentkit remove` - Remove installed artifacts
- `agentkit update` - Update installed artifacts to their latest upstream version
- `agentkit sync [URL]` - Sync artifacts from a repository
- `agentkit pack [URL]` - Pack artifacts into zip files
- `agentkit validate` - Validate artifacts format
- `agentkit doctor` - Diagnose artifacts directory issues

## Development
```bash
# Install in development mode
pip install -e .

# Run tests
pytest

# Run directly
python -m agentkit.cli
```



