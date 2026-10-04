"""Validation and redaction of run sources (git URLs and local paths)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.models.agents import SourceKind

# Only transports that cannot execute local commands. "-" prefixes can never match,
# so the URL cannot be read as a git option.
GIT_URL = re.compile(r"^(https://\S+|ssh://\S+|git@[\w.-]+:\S+)$")


class InvalidSource(ValueError):
    pass


def classify_source(source: str) -> tuple[SourceKind, str]:
    """Return the source kind and the normalized fetch target, or raise InvalidSource."""
    source = source.strip()
    if GIT_URL.match(source):
        return SourceKind.GIT, source
    if re.match(r"^[a-z][a-z0-9+.-]*://", source, re.IGNORECASE) or source.startswith("git@"):
        raise InvalidSource("Git URLs must use https://, ssh:// or git@host:path")
    path = Path(source).expanduser()
    if not path.is_dir():
        raise InvalidSource(f"Not a git URL or an existing local directory: {source}")
    return SourceKind.LOCAL, str(path.resolve())


def redact(url: str) -> str:
    """Strip credentials (e.g. a token in https://user:token@host/...) before a URL is stored or logged."""
    if not url.startswith(("https://", "ssh://")):
        return url
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))


def redact_in(text: str, secret: str) -> str:
    """Replace every occurrence of ``secret`` (and its credential part) in free text, e.g. git stderr."""
    if not secret:
        return text
    text = text.replace(secret, redact(secret))
    if secret.startswith(("https://", "ssh://")):
        userinfo = urlsplit(secret).netloc.rpartition("@")[0]
        if userinfo:
            text = text.replace(userinfo, "***")
            for part in userinfo.split(":"):
                if len(part) >= 4:
                    text = text.replace(part, "***")
    return text
