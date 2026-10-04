"""OWASP Top 10 (2021) categories and the mapping from bandit results onto them.

Bandit test ids are the primary key; a result's CWE is the fallback for tests not listed.
"""

from __future__ import annotations

OWASP_NAMES = {
    "A01:2021": "Broken Access Control",
    "A02:2021": "Cryptographic Failures",
    "A03:2021": "Injection",
    "A04:2021": "Insecure Design",
    "A05:2021": "Security Misconfiguration",
    "A06:2021": "Vulnerable and Outdated Components",
    "A07:2021": "Identification and Authentication Failures",
    "A08:2021": "Software and Data Integrity Failures",
    "A09:2021": "Security Logging and Monitoring Failures",
    "A10:2021": "Server-Side Request Forgery",
}

_BY_TEST: dict[str, str] = {
    # misconfiguration / access
    "B103": "A01:2021",  # permissive file mask
    "B108": "A01:2021",  # hardcoded /tmp path
    "B306": "A01:2021",  # mktemp
    "B104": "A05:2021",  # bind to all interfaces
    "B201": "A05:2021",  # flask debug
    "B113": "A05:2021",  # request without timeout
    # crypto
    "B303": "A02:2021", "B304": "A02:2021", "B305": "A02:2021", "B311": "A02:2021", "B324": "A02:2021",
    "B501": "A02:2021", "B502": "A02:2021", "B503": "A02:2021", "B504": "A02:2021", "B505": "A02:2021",
    "B507": "A02:2021", "B323": "A02:2021",
    # authentication / secrets
    "B105": "A07:2021", "B106": "A07:2021", "B107": "A07:2021", "B612": "A07:2021",
    # integrity: unsafe deserialization and loading
    "B301": "A08:2021", "B302": "A08:2021", "B506": "A08:2021", "B614": "A08:2021",
    # logging
    "B110": "A09:2021", "B112": "A09:2021",
    # SSRF
    "B310": "A10:2021",
    # injection: eval/exec, shell, SQL, templates, XSS
    "B102": "A03:2021", "B307": "A03:2021", "B308": "A03:2021", "B308b": "A03:2021",
    "B601": "A03:2021", "B602": "A03:2021", "B603": "A03:2021", "B604": "A03:2021", "B605": "A03:2021",
    "B606": "A03:2021", "B607": "A03:2021", "B608": "A03:2021", "B609": "A03:2021",
    "B610": "A03:2021", "B611": "A03:2021", "B701": "A03:2021", "B702": "A03:2021", "B703": "A03:2021",
    # XML parsers (XXE)
    "B313": "A05:2021", "B314": "A05:2021", "B315": "A05:2021", "B316": "A05:2021", "B317": "A05:2021",
    "B318": "A05:2021", "B319": "A05:2021", "B320": "A05:2021",
}

# Notes that are not weaknesses in themselves: `assert` use, and "this module is risky" import notices
# (B401-B415), which only duplicate the call-site checks above and would double every finding.
_IGNORED = {"B101", *(f"B4{n:02d}" for n in range(1, 16))}

_BY_CWE: dict[int, str] = {
    22: "A01:2021", 73: "A01:2021", 284: "A01:2021", 732: "A01:2021", 377: "A01:2021",
    78: "A03:2021", 79: "A03:2021", 89: "A03:2021", 94: "A03:2021", 95: "A03:2021", 77: "A03:2021",
    259: "A07:2021", 798: "A07:2021", 287: "A07:2021",
    295: "A02:2021", 326: "A02:2021", 327: "A02:2021", 328: "A02:2021", 330: "A02:2021", 338: "A02:2021",
    502: "A08:2021", 611: "A05:2021", 776: "A05:2021", 918: "A10:2021", 400: "A05:2021",
}


def owasp_category(test_id: str, cwe: int | None) -> str | None:
    """OWASP Top 10 id for a bandit result, or None when it is a code-quality note (e.g. assert used)."""
    if test_id in _IGNORED:
        return None
    return _BY_TEST.get(test_id) or (_BY_CWE.get(cwe) if cwe is not None else None)


def owasp_name(category: str | None) -> str | None:
    return OWASP_NAMES.get(category) if category else None
