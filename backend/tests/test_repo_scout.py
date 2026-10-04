from __future__ import annotations

from pathlib import Path

import pytest
from git import Actor, Repo

from app.agents.repo_scout import FetchError, clone_repository, fetch_source
from app.models.agents import SourceKind


@pytest.fixture
def git_repo(tmp_path: Path) -> Repo:
    path = tmp_path / "origin"
    repo = Repo.init(path, initial_branch="main")
    (path / "app.py").write_text("def main():\n    return 1\n")
    repo.index.add(["app.py"])
    who = Actor("Test", "test@example.com")
    repo.index.commit("initial", author=who, committer=who)
    return repo


def test_clone_with_gitpython(tmp_path, git_repo):
    dest = tmp_path / "clone"
    clone = clone_repository(Path(git_repo.working_dir).as_uri(), dest, allow_local=True)

    assert (dest / "app.py").is_file()
    assert clone.head.commit.hexsha == git_repo.head.commit.hexsha


def test_local_file_urls_are_refused_by_default(tmp_path, git_repo):
    with pytest.raises(FetchError, match="git clone of file://"):
        clone_repository(Path(git_repo.working_dir).as_uri(), tmp_path / "clone")


def test_local_copy_reports_git_metadata_and_skips_junk(tmp_path, git_repo):
    origin = Path(git_repo.working_dir)
    (origin / "__pycache__").mkdir()
    (origin / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"junk")
    dest = tmp_path / "copy"

    commit, branch = fetch_source(SourceKind.LOCAL, str(origin), dest)

    assert (commit, branch) == (git_repo.head.commit.hexsha, "main")
    assert (dest / "app.py").is_file()
    assert not (dest / ".git").exists() and not (dest / "__pycache__").exists()


def test_fetch_replaces_leftovers_from_a_failed_attempt(tmp_path, git_repo):
    dest = tmp_path / "copy"
    dest.mkdir()
    (dest / "stale.py").write_text("half-copied")

    fetch_source(SourceKind.LOCAL, git_repo.working_dir, dest)

    assert not (dest / "stale.py").exists()
    assert (dest / "app.py").is_file()


def test_plain_directory_has_no_git_metadata(tmp_path):
    src = tmp_path / "plain"
    src.mkdir()
    (src / "x.py").write_text("x = 1\n")
    assert fetch_source(SourceKind.LOCAL, str(src), tmp_path / "copy") == (None, None)
