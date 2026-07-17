#!/usr/bin/env python3
"""Drain a binary stream while emitting no more than a fixed byte budget."""

from __future__ import annotations

import sys
from typing import BinaryIO, Sequence


TRUNCATION_MARKER = b"\n[output truncated]"
_MAX_READ_CHUNK_BYTES = 64 * 1024


def drain_bounded_stream(
        source: BinaryIO, destination: BinaryIO, max_bytes: int) -> bool:
    """Drain ``source`` to EOF and emit at most ``max_bytes`` to ``destination``.

    The retained bytearray never exceeds the configured output budget. Input
    after that budget is still consumed so the upstream Docker process cannot
    block on a full pipe. The return value records whether truncation occurred.
    """
    if max_bytes < len(TRUNCATION_MARKER):
        raise ValueError("output budget is smaller than the truncation marker")

    retained = bytearray()
    truncated = False
    read_size = min(_MAX_READ_CHUNK_BYTES, max_bytes)

    while True:
        chunk = source.read(read_size)
        if not chunk:
            break
        remaining = max_bytes - len(retained)
        if remaining:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True

    if truncated:
        prefix_bytes = max_bytes - len(TRUNCATION_MARKER)
        emitted = bytes(retained[:prefix_bytes]) + TRUNCATION_MARKER
    else:
        emitted = bytes(retained)
    destination.write(emitted)
    destination.flush()
    return truncated


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bounded binary copy using one positive byte-budget argument."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("ERROR: expected one output byte budget", file=sys.stderr)
        return 2
    try:
        max_bytes = int(args[0])
        if max_bytes <= 0:
            raise ValueError("output budget must be positive")
        drain_bounded_stream(sys.stdin.buffer, sys.stdout.buffer, max_bytes)
    except (OSError, ValueError) as exc:
        print(
            f"ERROR: bounded output filter failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
