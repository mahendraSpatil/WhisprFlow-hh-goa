"""A small in-memory GitHub REST server, so the real PyGithub client can be exercised without the network.

It implements just the endpoints CodeLoop uses (repos, branches, contents, the Git Data API, pulls) with
git-like semantics: commits, nested trees with file modes, refs. It records every request so tests can
assert on what was sent, and can inject failures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

TOKEN = "ghp_test_token_0123456789"


def _sha(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Recorded:
    method: str
    path: str
    authorization: str | None
    body: dict | None


@dataclass
class FakeRepo:
    full_name: str
    default_branch: str = "main"
    blobs: dict[str, bytes] = field(default_factory=dict)
    commits: dict[str, dict] = field(default_factory=dict)  # sha -> {tree, parents, message}
    trees: dict[str, dict] = field(default_factory=dict)  # sha -> {"flat": {path: (mode, blob)}, "prefix": str}
    branches: dict[str, str] = field(default_factory=dict)  # name -> commit sha
    pulls: list[dict] = field(default_factory=list)

    def put_tree(self, flat: dict[str, tuple[str, str]], prefix: str = "") -> str:
        scoped = {p: v for p, v in flat.items() if p.startswith(prefix)}
        sha = _sha("tree", prefix, sorted(scoped.items()))
        self.trees[sha] = {"flat": flat, "prefix": prefix}
        return sha

    def put_blob(self, data: bytes) -> str:
        sha = _sha("blob", data.hex())
        self.blobs[sha] = data
        return sha

    def commit(self, flat: dict[str, tuple[str, str]], parents: list[str], message: str) -> str:
        tree = self.put_tree(flat)
        sha = _sha("commit", tree, parents, message)
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def seed(self, files: dict[str, bytes | str], modes: dict[str, str] | None = None, branch: str | None = None) -> str:
        flat = {}
        for path, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            flat[path] = ((modes or {}).get(path, "100644"), self.put_blob(data))
        sha = self.commit(flat, [], "initial commit")
        self.branches[branch or self.default_branch] = sha
        return sha

    def files_at(self, commit_sha: str) -> dict[str, tuple[str, bytes]]:
        flat = self.trees[self.commits[commit_sha]["tree"]]["flat"]
        return {p: (mode, self.blobs[blob]) for p, (mode, blob) in flat.items()}


class FakeGitHub:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[Recorded] = []
        self.failures: list[tuple[str, str, int, dict]] = []  # (method, path regex, status, json body)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # lifecycle ----------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> FakeGitHub:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add_repo(self, full_name: str, **kwargs) -> FakeRepo:
        self.repos[full_name] = FakeRepo(full_name, **kwargs)
        return self.repos[full_name]

    def fail(self, method: str, path_regex: str, status: int, message: str = "boom", headers: dict | None = None) -> None:
        self.failures.append((method, path_regex, status, {"message": message, "_headers": headers or {}}))

    def calls(self, method: str, path_regex: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and re.search(path_regex, r.path)]

    # protocol -----------------------------------------------------------------------------------
    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            wbufsize = 1 << 16  # headers and body in one write: separate small writes stall ~200ms on Nagle + delayed ACK

            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, payload, headers: dict | None = None) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                self.wfile.flush()

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append(Recorded(method, parsed.path, self.headers.get("Authorization"), body))
                if self.headers.get("Authorization") not in (f"token {fake.token}", f"Bearer {fake.token}"):
                    return self._send(401, {"message": "Bad credentials"})
                for m, regex, status, payload in fake.failures:
                    if m == method and re.search(regex, parsed.path):
                        extra = dict(payload.get("_headers", {}))
                        return self._send(status, {"message": payload["message"]}, extra)
                try:
                    status, payload = fake.route(method, parsed.path, parse_qs(parsed.query), body)
                except KeyError:
                    status, payload = 404, {"message": "Not Found"}
                self._send(status, payload)

            def do_GET(self):
                self._dispatch("GET")

            def do_POST(self):
                self._dispatch("POST")

        return Handler

    # routing ------------------------------------------------------------------------------------
    def _repo_url(self, repo: FakeRepo) -> str:
        return f"{self.url}/repos/{repo.full_name}"

    def route(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, object]:
        match = re.match(r"^/repos/([^/]+/[^/]+)(?:/(.*))?$", path)
        if not match:
            return 404, {"message": "Not Found"}
        repo = self.repos[match[1]]  # KeyError -> 404
        rest = unquote(match[2] or "")
        base = self._repo_url(repo)

        if method == "GET" and rest == "":
            owner, name = repo.full_name.split("/")
            return 200, {
                "id": 1, "name": name, "full_name": repo.full_name, "url": base, "default_branch": repo.default_branch,
                "html_url": f"https://github.com/{repo.full_name}", "owner": {"login": owner, "type": "User"},
            }
        if method == "GET" and rest.startswith("branches/"):
            name = rest[len("branches/"):]
            sha = repo.branches[name]
            return 200, {"name": name, "commit": {"sha": sha, "url": f"{base}/commits/{sha}"}, "protected": False}
        if method == "GET" and rest.startswith("git/commits/"):
            sha = rest.rsplit("/", 1)[1]
            c = repo.commits[sha]
            return 200, self._commit_json(repo, sha, c)
        if method == "GET" and rest.startswith("git/trees/"):
            return 200, self._tree_json(repo, rest.rsplit("/", 1)[1])
        if method == "GET" and rest.startswith("contents/"):
            return self._contents(repo, rest[len("contents/"):], query.get("ref", [repo.branches[repo.default_branch]])[0])
        if method == "GET" and rest == "pulls":
            head = query.get("head", [None])[0]
            state = query.get("state", ["open"])[0]
            return 200, [p for p in repo.pulls if (head is None or p["head"]["label"] == head) and (state == "all" or p["state"] == state)]

        if method == "POST" and rest == "git/blobs":
            assert body is not None
            data = base64.b64decode(body["content"]) if body.get("encoding") == "base64" else body["content"].encode()
            sha = repo.put_blob(data)
            return 201, {"sha": sha, "url": f"{base}/git/blobs/{sha}"}
        if method == "POST" and rest == "git/trees":
            assert body is not None
            flat = dict(repo.trees[body["base_tree"]]["flat"]) if body.get("base_tree") else {}
            for element in body["tree"]:
                flat[element["path"]] = (element["mode"], element["sha"])
                assert element["sha"] in repo.blobs, "tree element points at an unknown blob"
            return 201, self._tree_json(repo, repo.put_tree(flat))
        if method == "POST" and rest == "git/commits":
            assert body is not None
            tree = repo.trees[body["tree"]]
            sha = _sha("commit", body["tree"], body["parents"], body["message"])
            repo.commits[sha] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
            assert all(p in repo.commits for p in body["parents"])
            del tree
            return 201, self._commit_json(repo, sha, repo.commits[sha])
        if method == "POST" and rest == "git/refs":
            assert body is not None
            name = body["ref"].removeprefix("refs/heads/")
            if name in repo.branches:
                return 422, {"message": "Reference already exists"}
            if body["sha"] not in repo.commits:
                return 422, {"message": "Object does not exist"}
            repo.branches[name] = body["sha"]
            return 201, {"ref": body["ref"], "url": f"{base}/git/{body['ref']}", "object": {"sha": body["sha"], "type": "commit"}}
        if method == "POST" and rest == "pulls":
            assert body is not None
            if body["head"] not in repo.branches or body["base"] not in repo.branches:
                return 422, {"message": "Validation Failed"}
            number = len(repo.pulls) + 1
            owner = repo.full_name.split("/")[0]
            pr = {
                "number": number, "state": "open", "title": body["title"], "body": body.get("body", ""),
                "draft": bool(body.get("draft", False)), "url": f"{base}/pulls/{number}",
                "html_url": f"https://github.com/{repo.full_name}/pull/{number}",
                "head": {"ref": body["head"], "label": f"{owner}:{body['head']}", "sha": repo.branches[body["head"]]},
                "base": {"ref": body["base"], "label": f"{owner}:{body['base']}", "sha": repo.branches[body["base"]]},
            }
            repo.pulls.append(pr)
            return 201, pr
        return 404, {"message": "Not Found"}

    def _commit_json(self, repo: FakeRepo, sha: str, c: dict) -> dict:
        base = self._repo_url(repo)
        return {
            "sha": sha, "url": f"{base}/git/commits/{sha}", "message": c["message"],
            "tree": {"sha": c["tree"], "url": f"{base}/git/trees/{c['tree']}"},
            "parents": [{"sha": p, "url": f"{base}/git/commits/{p}"} for p in c["parents"]],
        }

    def _tree_json(self, repo: FakeRepo, sha: str) -> dict:
        info = repo.trees[sha]
        flat, prefix = info["flat"], info["prefix"]
        entries: dict[str, dict] = {}
        for path, (mode, blob) in sorted(flat.items()):
            if not path.startswith(prefix):
                continue
            head, _, tail = path[len(prefix):].partition("/")
            if tail:
                entries.setdefault(head, {
                    "path": head, "mode": "040000", "type": "tree",
                    "sha": repo.put_tree(flat, f"{prefix}{head}/"),
                })
            else:
                entries[head] = {"path": head, "mode": mode, "type": "blob", "sha": blob, "size": len(repo.blobs[blob])}
        base = self._repo_url(repo)
        for e in entries.values():
            e["url"] = f"{base}/git/{e['type']}s/{e['sha']}"
        return {"sha": sha, "url": f"{base}/git/trees/{sha}", "tree": list(entries.values()), "truncated": False}

    def _contents(self, repo: FakeRepo, path: str, ref: str) -> tuple[int, object]:
        sha = repo.branches.get(ref, ref)
        flat = repo.trees[repo.commits[sha]["tree"]]["flat"]
        mode, blob = flat[path]  # KeyError -> 404
        data = repo.blobs[blob]
        base = self._repo_url(repo)
        return 200, {
            "type": "file", "encoding": "base64", "name": path.rsplit("/", 1)[-1], "path": path, "sha": blob, "size": len(data),
            "content": base64.encodebytes(data).decode(), "url": f"{base}/contents/{path}?ref={sha}",
            "git_url": f"{base}/git/blobs/{blob}", "html_url": f"https://github.com/{repo.full_name}/blob/{sha}/{path}",
            "download_url": None,
        }
