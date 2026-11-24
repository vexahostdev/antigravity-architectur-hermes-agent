"""Dual-scope security scanner for memory entries and skill files."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

MAX_SCAN_CHARS = 65_536

_FILLER = r"(?:\w+\s+){0,8}"
_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"

# Invisible and bidirectional override characters (leading BOM at index 0 is tolerated).
_INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)

_RAW_PATTERNS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    # Classic prompt injection (both strict & skill)
    (rf"ignore\s+{_FILLER}(?:previous|all|above|prior)\s+{_FILLER}instructions", "prompt_injection", ("strict", "skill")),
    (r"system\s+prompt\s+override", "sys_prompt_override", ("strict", "skill")),
    (rf"disregard\s+{_FILLER}(?:your|all|any|previous|system)\s+{_FILLER}(?:instructions|rules|guidelines|prompt)", "disregard_rules", ("strict", "skill")),
    (rf"act\s+as\s+(?:if|though)\s+{_FILLER}you\s+{_FILLER}(?:have\s+no|don't\s+have)\s+{_FILLER}(?:restrictions|limits|rules)", "bypass_restrictions", ("strict", "skill")),
    (r"<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->", "html_comment_injection", ("strict", "skill")),
    (r"<\s*div\s+style\s*=\s*[\"'][^>]{0,2048}display\s*:\s*none", "hidden_div", ("strict", "skill")),
    (rf"do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user", "deception_hide", ("strict", "skill")),

    # Exfiltration & RCE
    (rf"curl\s+[^\n]{{0,2048}}{_SECRET_VAR}", "exfil_curl", ("strict", "skill")),
    (rf"wget\s+[^\n]{{0,2048}}{_SECRET_VAR}", "exfil_wget", ("strict", "skill")),
    (r"\bcat\s+[^\n]{0,2048}(?:\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)\b", "read_secrets", ("strict", "skill")),
    (rf"(?:include|output|print|share)\s+{_FILLER}(?:conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)", "context_exfil", ("strict", "skill")),
    (r"\b(?:curl|wget)\s+[^|\n]*\|\s*(?:ba)?sh\b", "remote_shell_pipe", ("strict",)),
    (r"\bpowershell(?:\.exe)?\s+[^\n]*-(?:e|enc|encodedcommand)\b", "encoded_powershell", ("strict",)),
    (r"\beval\s*\(\s*base64_decode\b", "obfuscated_eval", ("strict",)),

    # Persistence / SSH backdoors & hardcoded secrets
    (r"\bauthorized_keys\b", "ssh_backdoor", ("strict", "skill")),
    (
        r"(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write"
        r"|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)"
        r"[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)",
        "ssh_access",
        ("strict", "skill"),
    ),
    (r"\$HOME/\.hermes/\.env|~/\.hermes/\.env", "hermes_env_access", ("strict", "skill")),
    (r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*[\"'][A-Za-z0-9+/=_-]{20,}[\"']", "hardcoded_secret", ("strict", "skill")),

    # Strict-only (memory store)
    (r"you\s+are\s+now\s+(?:in\s+)?(?:developer|jailbreak|dan)\s+mode", "jailbreak_mode", ("strict",)),
    (r"(?:send|post|upload|transmit)\s+[^\n]{0,2048}\s+(?:to|at)\s+https?://", "send_to_url", ("strict",)),
)

_COMPILED_BY_SCOPE = {
    scope: [
        (re.compile(pat, re.IGNORECASE), pid)
        for pat, pid, scopes in _RAW_PATTERNS
        if scope in scopes
    ]
    for scope in ("strict", "skill")
}


def scan_content(text: str, scope: str = "strict") -> List[str]:
    """Return list of matched security threat pattern IDs (empty if clean)."""
    if not text:
        return []
    sample = text[:MAX_SCAN_CHARS]
    findings: List[str] = []

    if any(ch in _INVISIBLE_CHARS for ch in sample) or ("\ufeff" in sample[1:]):
        findings.append("invisible_unicode")

    patterns = _COMPILED_BY_SCOPE.get(scope, _COMPILED_BY_SCOPE["strict"])
    for regex, pid in patterns:
        if regex.search(sample):
            findings.append(pid)
    return list(dict.fromkeys(findings))


def first_threat_error(text: str, scope: str = "strict") -> Optional[str]:
    """Return a concise refusal message if *text* triggers any pattern in *scope*, else None."""
    findings = scan_content(text, scope=scope)
    if not findings:
        return None
    return f"Refused: content matched security threat pattern(s): {', '.join(findings)}."
