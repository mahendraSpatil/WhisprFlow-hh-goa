"""The diff plumbing, against real `git apply`."""

from __future__ import annotations

import pytest
from helpers import make_diff, write_files

from app.agents.patching import check_patch, parse_response, validate_diff

ORIGINAL = "def ratio(n):\n    return 100 // n\n\n\ndef other():\n    return 1\n"
FIXED = ORIGINAL.replace("return 100 // n", "return 100 // n if n else 0")


@pytest.fixture
def repo(tmp_path):
    write_files(tmp_path, {"app/calc.py": ORIGINAL, "README.md": "hello\n"})
    return tmp_path


# --- Parsing Claude's reply -------------------------------------------------------------


def test_parse_diff_and_design():
    diff, design = parse_response("<diff>\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n</diff>\n\n<design>\nUse a lock.\nAlways.\n</design>")
    assert diff == "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert design == "Use a lock. Always."


def test_parse_tolerates_fences_unclosed_tags_and_missing_sections():
    fenced, _ = parse_response("<diff>\n```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```\n</diff>")
    assert fenced.startswith("--- a/x.py\n") and "```" not in fenced

    unclosed, design = parse_response("<diff>\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n<design>Parameterize.</design>")
    assert unclosed.endswith("+b\n") and "<design>" not in unclosed and design == "Parameterize."

    assert parse_response("Sorry, no.") == (None, "")
    assert parse_response("<diff>\n\n</diff><design>d</design>") == (None, "d")


# --- Path validation ---------------------------------------------------------------------


def header(old, new):
    return f"--- {old}\n+++ {new}\n@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize(
    "diff, message",
    [
        ("not a diff", "no `--- a/<path>`"),
        (header("a/app/calc.py", "b/app/other.py"), "renames"),
        (header("/dev/null", "b/app/new.py"), "creates or deletes"),
        (header("a/app/calc.py", "/dev/null"), "creates or deletes"),
        (header("a/../outside.py", "b/../outside.py"), "not relative"),
        (header("a//etc/passwd", "b//etc/passwd"), "not relative"),
        (header("a/C:/Windows/win.ini", "b/C:/Windows/win.ini"), "not relative"),
        (header("a/app\\calc.py", "b/app\\calc.py"), "not relative"),
        (header("a/app/missing.py", "b/app/missing.py"), "not an existing regular file"),
        (header("a/app", "b/app"), "not an existing regular file"),
        ("new file mode 100644\n" + header("a/app/calc.py", "b/app/calc.py"), "adds, deletes, renames"),
        ("GIT binary patch\n" + header("a/app/calc.py", "b/app/calc.py"), "adds, deletes, renames"),
        pytest.param("x" * 200_001, "too large", id="oversized-diff"),
    ],
)
def test_validation_rejects_anything_but_in_place_edits(repo, diff, message):
    files, error = validate_diff(diff, repo)
    assert files == [] and message in error


def test_validation_accepts_in_place_edits_and_limits_file_count(repo):
    assert validate_diff(header("a/app/calc.py", "b/app/calc.py"), repo) == (["app/calc.py"], None)
    assert validate_diff(header("app/calc.py", "app/calc.py"), repo)[0] == ["app/calc.py"]  # no a/ b/ prefixes

    for name in ("b.py", "c.py", "d.py"):
        (repo / name).write_text("x\n")
    many = "".join(header(f"a/{n}", f"b/{n}") for n in ("app/calc.py", "b.py", "c.py", "d.py"))
    assert "limit" in validate_diff(many, repo)[1]


def test_validation_refuses_symlinks(repo):
    try:
        (repo / "link.py").symlink_to(repo / "app" / "calc.py")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert validate_diff(header("a/link.py", "b/link.py"), repo)[0] == []


# --- git apply --check -------------------------------------------------------------------


def test_a_good_diff_applies_and_yields_both_files_without_touching_the_working_copy(repo):
    diff = make_diff("app/calc.py", ORIGINAL, FIXED)
    check = check_patch(repo, diff, ["app/calc.py"])

    assert check.ok and check.error == ""
    (file,) = check.files
    assert (file.path, file.original, file.patched) == ("app/calc.py", ORIGINAL, FIXED)
    assert (repo / "app" / "calc.py").read_text() == ORIGINAL  # nothing was written


def test_wrong_hunk_counts_are_forgiven_but_wrong_context_is_not(repo):
    diff = make_diff("app/calc.py", ORIGINAL, FIXED)
    miscounted = diff.replace("@@ -1,", "@@ -9,").replace(" +1,", " +9,")  # wrong start lines
    assert check_patch(repo, miscounted, ["app/calc.py"]).ok  # git finds the context at an offset

    import re

    garbled = re.sub(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", "@@ -1,99 +1,99 @@", diff)  # wrong counts (--recount fixes these)
    assert check_patch(repo, garbled, ["app/calc.py"]).ok

    stale = make_diff("app/calc.py", ORIGINAL.replace("100 //", "100 /"), FIXED)  # context does not match the file
    check = check_patch(repo, stale, ["app/calc.py"])
    assert not check.ok and "patch does not apply" in check.error


def test_a_patch_that_breaks_syntax_is_rejected_even_though_it_applies(repo):
    broken = ORIGINAL.replace("return 100 // n", "return 100 //")
    check = check_patch(repo, make_diff("app/calc.py", ORIGINAL, broken), ["app/calc.py"])
    assert not check.ok and "syntax error" in check.error and "app/calc.py" in check.error


def test_non_python_files_skip_the_syntax_check(repo):
    check = check_patch(repo, make_diff("README.md", "hello\n", "hello world\n"), ["README.md"])
    assert check.ok and check.files[0].patched == "hello world\n"


def test_crlf_files_accept_diffs_written_with_lf(tmp_path):
    (tmp_path / "w.py").write_bytes(ORIGINAL.replace("\n", "\r\n").encode())
    check = check_patch(tmp_path, make_diff("w.py", ORIGINAL, FIXED), ["w.py"])
    assert check.ok
