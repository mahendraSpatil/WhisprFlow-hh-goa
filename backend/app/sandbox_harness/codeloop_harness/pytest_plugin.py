"""pytest plugin loaded with ``-p codeloop_harness.pytest_plugin``.

Applies the sandbox guards and streams one structured result per test to the
private channel, so results survive even if the run is killed on timeout.
"""

import os
import time

import pytest

from codeloop_harness import encode, guard, open_channel

_ROOT = os.path.realpath(os.environ.get("CODELOOP_REPO_ROOT", os.getcwd()))
_channel = open_channel()  # taken at import, before pytest starts capturing output
_pending: dict = {}


def _emit(event: dict) -> None:
    _channel.write(encode(event))


def qualified(frame_globals: dict, code) -> str:
    qualname = getattr(code, "co_qualname", code.co_name).replace(".<locals>", "")
    return f"{frame_globals.get('__name__', '?')}.{qualname}"


def describe(excinfo) -> dict:
    frames = []
    for entry in excinfo.traceback:
        path = os.path.realpath(str(entry.path))
        if not path.startswith(_ROOT + os.sep):
            continue
        frames.append(
            {
                "file": os.path.relpath(path, _ROOT).replace(os.sep, "/"),
                "line": entry.lineno + 1,
                "function": qualified(entry.frame.f_globals, entry.frame.code.raw),
            }
        )
    return {"type": excinfo.typename, "message": str(excinfo.value)[:2000], "frames": frames}


def pytest_configure(config):
    guard.install(_ROOT)
    _emit({"event": "session.start", "time": time.time()})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    state = _pending.setdefault(item.nodeid, {"outcome": "passed", "duration": 0.0, "message": None, "exception": None})
    state["duration"] += report.duration
    if report.when == "call" and hasattr(report, "wasxfail"):
        state["outcome"] = "skipped"
        state["message"] = f"xfail: {report.wasxfail}"
    elif report.skipped and state["outcome"] == "passed":
        state["outcome"] = "skipped"
        longrepr = report.longrepr
        state["message"] = str(longrepr[2] if isinstance(longrepr, tuple) else longrepr)[:500]
    elif report.failed and state["outcome"] in ("passed", "skipped"):
        state["outcome"] = "failed" if report.when == "call" else "error"
        if call.excinfo is not None:
            state["exception"] = describe(call.excinfo)
            state["message"] = f"{call.excinfo.typename}: {str(call.excinfo.value)[:500]}"


def pytest_runtest_logfinish(nodeid, location):
    state = _pending.pop(nodeid, None)
    if state is not None:
        _emit({"event": "test", "node_id": nodeid, **state})


def pytest_collectreport(report):
    if report.failed:
        _emit(
            {
                "event": "test",
                "node_id": report.nodeid or "<collection>",
                "outcome": "error",
                "duration": 0.0,
                "message": str(report.longrepr)[-1500:],
                "exception": None,
            }
        )


def pytest_sessionfinish(session, exitstatus):
    _emit({"event": "session.finish", "exitstatus": int(exitstatus)})
