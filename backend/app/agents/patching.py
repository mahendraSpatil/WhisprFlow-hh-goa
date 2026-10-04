"""Parsing, validating and applying the unified diffs Claude returns.

Diffs are untrusted model output built from untrusted repository text, so nothing is applied
to the real working copy: paths are validated first, the diff is checked with
``git apply --check`` against the working copy (read-only), and the patched text is produced in
a scratch directory that contains only the files the diff names.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.models.agents import PatchedFile

MAX_DIFF_CHARS = 200_000
MAX_FILES = 3
MAX_FILE_BYTES = 1_000_000
MAX_ERROR_CHARS = 1500
GIT_TIMEOUT_S = 30

# Anything that is not an in-place edit of an existing text file.
_UNSUPPORTED = re.compile(
    r"^(rename (from|to) |copy (from|to) |similarity index |dissimilarity index |new file mode |"
    r"deleted file mode |old mode |new mode |GIT binary patch|Binary files |Subproject commit )"
)
_DIFF_TAG = re.compile(r"<diff>(.*?)(?:</diff>|(?=<design>)|\Z)", re.DOTALL)
_DESIGN_TAG = re.compile(r"<design>(.*?)(?:</design>|\Z)", re.DOTALL)
_FENCE = re.compile(r"^\s*```[\w-]*\s*$")


@dataclass
class PatchCheck:
    ok: bool
    error: str = ""
    files: list[PatchedFile] = field(default_factory=list)


def parse_response(text: str) -> tuple[str | None, str]:
    """Split Claude's reply into (diff or None, design paragraph)."""
    diff_match, design_match = _DIFF_TAG.search(text), _DESIGN_TAG.search(text)
    design = " ".join((design_match.group(1) if design_match else "").split())
    if not diff_match:
        return None, design
    lines = diff_match.group(1).strip("\n").splitlines()
    while lines and (not lines[0].strip() or _FENCE.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _FENCE.match(lines[-1])):
        lines.pop()
    diff = "\n".join(lines)
    return (diff + "\n" if diff else None), design


def diff_paths(diff: str) -> list[tuple[str, str]]:
    """(old, new) paths from the file headers: a minus-minus-minus line immediately followed by a plus-plus-plus line."""
    lines = diff.splitlines()
    return [
        (line[4:].split("\t")[0].strip(), lines[i + 1][4:].split("\t")[0].strip())
        for i, line in enumerate(lines[:-1])
        if line.startswith("--- ") and lines[i + 1].startswith("+++ ")
    ]


def validate_diff(diff: str, root: Path) -> tuple[list[str], str | None]:
    """The files the diff edits, or an error message. Only in-place edits of existing files pass."""
    if len(diff) > MAX_DIFF_CHARS:
        return [], f"the diff is too large ({len(diff)} characters; the limit is {MAX_DIFF_CHARS})"
    if any(_UNSUPPORTED.match(line) for line in diff.splitlines()):
        return [], "the diff adds, deletes, renames or changes the mode of a file; only in-place edits are supported"
    headers = diff_paths(diff)
    if not headers:
        return [], "the diff has no `--- a/<path>` / `+++ b/<path>` file headers"
    base = root.resolve()
    files: list[str] = []
    for old, new in headers:
        if "/dev/null" in (old, new):
            return [], "the diff creates or deletes a file; only in-place edits are supported"
        rel = [raw[2:] if raw[:2] in ("a/", "b/") else raw for raw in (old, new)]
        if rel[0] != rel[1]:
            return [], f"the diff renames {rel[0]} to {rel[1]}; only in-place edits are supported"
        path = PurePosixPath(rel[0])
        if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in rel[0] or ":" in path.parts[0]:
            return [], f"the path {rel[0]!r} is not relative to the repository root"
        target = base.joinpath(*path.parts)
        try:
            resolved = target.resolve()
        except OSError:
            return [], f"cannot resolve {rel[0]}"
        if target.is_symlink() or base not in resolved.parents or not resolved.is_file():
            return [], f"{rel[0]} is not an existing regular file in the repository"
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return [], f"{rel[0]} is too large to patch"
        if str(path) not in files:
            files.append(str(path))
    if len(files) > MAX_FILES:
        return [], f"the diff touches {len(files)} files; the limit is {MAX_FILES}"
    return files, None


def _git_apply(cwd: Path, diff: str, *flags: str) -> tuple[bool, str]:
    """Run git apply on the diff; returns (succeeded, git's message). Raises on timeout or no git."""
    cmd = ["git", "apply", "--recount", *flags, "-"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    # Bytes, not text mode: on Windows text mode would turn every LF in the diff into CRLF, and an
    # LF diff would then fail to match LF files.
    result = subprocess.run(
        cmd, cwd=cwd, input=diff.encode("utf-8"), capture_output=True, env=env, timeout=GIT_TIMEOUT_S, check=False
    )
    message = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return result.returncode == 0, message[-MAX_ERROR_CHARS:]


def check_patch(root: Path, diff: str, files: list[str]) -> PatchCheck:
    """`git apply --check` against the working copy, then the patched text and a syntax check."""
    crlf = any(b"\r\n" in (root / f).read_bytes() for f in files)
    # Files with CRLF line endings are shown to the model with LF, so context lines only match when
    # whitespace at line ends is ignored.
    extra = ["--ignore-whitespace"] if crlf else []
    try:
        ok, message = _git_apply(root, diff, "--check", *extra)
    except subprocess.TimeoutExpired:
        return PatchCheck(False, "git apply --check timed out")
    except FileNotFoundError:
        return PatchCheck(False, "git is not installed or not on PATH")
    if not ok:
        return PatchCheck(False, message or "git apply --check failed")

    scratch = Path(tempfile.mkdtemp(prefix="codeloop-patch-"))
    try:
        for rel in files:
            (scratch / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / rel, scratch / rel)
        ok, message = _git_apply(scratch, diff, *extra)
        if not ok:  # only possible if the diff names a path the validation missed
            return PatchCheck(False, message)
        patched_files = []
        for rel in files:
            original = (root / rel).read_text(encoding="utf-8", errors="replace")
            patched = (scratch / rel).read_text(encoding="utf-8", errors="replace")
            if rel.endswith(".py"):
                try:
                    ast.parse(patched, filename=rel)
                except SyntaxError as exc:
                    return PatchCheck(False, f"the patched {rel} has a syntax error: {exc.msg} (line {exc.lineno})")
            patched_files.append(PatchedFile(path=rel, original=original, patched=patched))
        return PatchCheck(True, files=patched_files)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def apply_patch(root: Path, diff: str, paths: list[str]) -> tuple[str, str]:
    """Apply a diff to ``root`` in place: ("applied" | "redundant" | "failed", git's message).

    "redundant" means the diff does not apply but its *reverse* does: the change is already there,
    typically because an earlier patch made the same fix. Only ever call this on a throwaway copy.
    """
    crlf = any(b"\r\n" in (root / f).read_bytes() for f in paths if (root / f).is_file())
    extra = ["--ignore-whitespace"] if crlf else []
    try:
        ok, message = _git_apply(root, diff, *extra)
        if ok:
            return "applied", ""
        reverse_ok, _ = _git_apply(root, diff, "--reverse", "--check", *extra)
    except subprocess.TimeoutExpired:
        return "failed", "git apply timed out"
    except FileNotFoundError:
        return "failed", "git is not installed or not on PATH"
    return ("redundant", "") if reverse_ok else ("failed", message)
