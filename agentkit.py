#!/usr/bin/env python3
"""
AgentKit - Cross-platform CLI for managing Claude Code artifacts

Usage:
    python agentkit.py <command> [options]
    agentkit <command> [options]  # if installed

For help:
    python agentkit.py --help
"""

import sys

from agentkit import main

if __name__ == "__main__":
    sys.exit(main())
