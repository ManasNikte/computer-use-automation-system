"""
Redaction.

Two complementary mechanisms, because either alone is insufficient:

  * **Known-value redaction.** Values we were handed and know are sensitive
    (anything bound to an input param marked `sensitive`) are registered and
    masked by exact match wherever they appear. This is exact and reliable,
    and it is the only thing that protects a passcode, since a passcode looks
    like nothing in particular.

  * **Pattern redaction.** Regexes for the shapes regulated data takes --
    SSN/TIN, card numbers, account numbers, emails, phone numbers. This
    catches data we never passed in but *scraped off the screen*, which is
    the realistic leak path: a member detail page carries a tax ID whether or
    not we asked for it, and it lands in any page-text we log.

The redactor is installed at the logging sink and at the evidence writer, not
called ad hoc at each site. Anything that reaches disk goes through it, so a
new log statement cannot accidentally bypass it. That's the only design that
survives contact with a growing codebase.

Limits, stated honestly: pattern redaction cannot recognise a member *name*,
and a screenshot is a bitmap we do not OCR. See REPORT.md "Safety".
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Pattern, Tuple

MASK = "[REDACTED]"

# Ordered most-specific-first: a 9-digit TIN would otherwise be partly eaten
# by a looser numeric rule.
_PATTERNS: List[Tuple[str, Pattern]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("account", re.compile(r"\b(?:SV|CK|CD|XX)-\d{4,}-\d{2}\b")),
    ("email", re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"\b\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")),
    ("bearer", re.compile(r"\b(?:sk|gsk|xoxb|ghp)_[A-Za-z0-9]{8,}\b")),
]

# Keys whose values are masked wholesale regardless of shape.
_SENSITIVE_KEYS = {
    "password", "passcode", "secret", "token", "api_key", "apikey",
    "authorization", "tax_id", "ssn", "credential", "credentials",
}


class Redactor:
    """Redacts known secret values and regulated-data patterns from anything
    on its way to disk or to a console."""

    def __init__(self, patterns: Iterable[Tuple[str, Pattern]] = None):
        self._secrets: List[str] = []
        self._patterns = list(patterns) if patterns is not None else list(_PATTERNS)

    def register_secret(self, value: str) -> None:
        """Register a literal value to mask on sight.

        Short values are ignored: masking every occurrence of a 2-character
        string would shred the logs and protect nothing.
        """
        if value and isinstance(value, str) and len(value) >= 4:
            if value not in self._secrets:
                self._secrets.append(value)

    def register_secrets(self, values: Iterable[str]) -> None:
        for v in values:
            self.register_secret(v)

    def redact(self, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        out = text
        # Exact secrets first -- a passcode that happens to look like a phone
        # number should be masked as a secret either way, but ordering keeps
        # the result stable.
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, MASK)
        for _name, pattern in self._patterns:
            out = pattern.sub(MASK, out)
        return out

    def redact_obj(self, obj: Any) -> Any:
        """Recursively redact a JSON-shaped structure.

        Dict keys matching `_SENSITIVE_KEYS` have their values masked outright
        rather than pattern-matched, because `{"passcode": "hunter2"}` leaks
        even though "hunter2" matches nothing.
        """
        if isinstance(obj, dict):
            out: Dict[Any, Any] = {}
            for key, value in obj.items():
                if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                    out[key] = MASK
                else:
                    out[key] = self.redact_obj(value)
            return out
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return self.redact(obj)

    def redact_params(self, params: Dict[str, Any], sensitive_names: Iterable[str]) -> Dict[str, Any]:
        """Mask params the capability declared sensitive, plus anything the
        generic rules catch."""
        sensitive = set(sensitive_names or ())
        out: Dict[str, Any] = {}
        for key, value in (params or {}).items():
            if key in sensitive or (isinstance(key, str) and key.lower() in _SENSITIVE_KEYS):
                out[key] = MASK
            else:
                out[key] = self.redact_obj(value)
        return out
