"""Runs inside the CodeLoop sandbox next to the target repo. Standard library only.

Everything here executes with the target's interpreter (a fresh venv or a
python:slim container), never in the backend process.
"""

import json
import os

PREFIX = "@@codeloop "


def open_channel():
    """Where structured results are written, one JSON line per event.

    Normally a file named by CODELOOP_EVENTS: it cannot interleave with pytest's own terminal
    output, and every line is flushed, so results survive the run being killed on timeout.
    Falls back to a private copy of the real stdout, taken before anything can redirect it.
    """
    path = os.environ.get("CODELOOP_EVENTS")
    if path:
        return open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    return os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="replace")


def encode(event: dict) -> str:
    return PREFIX + json.dumps(event, default=repr) + "\n"
