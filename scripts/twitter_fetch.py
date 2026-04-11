#!/usr/bin/env python3
"""
Twitter/X Fetch Script — Stub
==============================
This is a no-op placeholder. The agent calls this script during Phases 2-3
to search Twitter/X for context, but the real implementation is not bundled
because common approaches (cookie-based access to X's internal GraphQL API)
raise Terms of Service concerns.

To enable Twitter research, replace this file with your own implementation.
See README.md "Twitter/X Integration" for the expected interface.

This stub returns empty results so the agent runs without errors.
"""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description="Twitter/X fetch (stub)")
    sub = parser.add_subparsers(dest="command")

    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)

    user = sub.add_parser("user")
    user.add_argument("--username", required=True)
    user.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()

    # Return empty results for all commands
    print(json.dumps([]))


if __name__ == "__main__":
    main()
