"""What agents type into fields.

Two reasons this is a fixed vocabulary rather than "whatever the model writes":

1. Credentials. A model can ask for ``user1_password``; the runner substitutes the real value
   from ``feena.yaml``. The secret never needs to be in the prompt, and macros/reports store the
   *reference*, not the value.
2. Cheap deciders. Choosing a value *kind* is a classification, so a non-generative decision
   model can drive the clumsy agent's bad-input probing. Generative models may still pass a
   literal ``value``; it is used as-is (after redacting anything that matches a secret).
"""
from __future__ import annotations

import re

VALUE_KINDS: dict[str, str] = {
    "valid_text": "Feena test entry",
    "valid_email": "feena.tester@example.test",
    "valid_password": "Feena-test-pass-1",
    "number": "42",
    "negative_number": "-1",
    "empty": "",
    "whitespace": "   ",
    "long": "x" * 5000,
    "unicode": "Zoë Ñandú 测试 עברית",
    "emoji": "🙂🙂🙂",
    "html": "<b>feena</b><img src=x>",
    "sqlish": "' OR '1'='1",
}

_SECRET_FIELDS = ("email", "password")


def secret_kinds(n_users: int) -> list[str]:
    return [f"user{i}_{f}" for i in range(1, n_users + 1) for f in _SECRET_FIELDS]


def is_secret(kind: str) -> bool:
    return bool(re.fullmatch(r"user[1-9][0-9]*_(?:email|password)", kind))


def all_kinds(n_users: int) -> list[str]:
    return list(VALUE_KINDS) + secret_kinds(n_users)


def resolve(kind: str, users) -> str | None:
    """Concrete text for a kind, or None if the kind is unknown/unavailable."""
    if kind in VALUE_KINDS:
        return VALUE_KINDS[kind]
    if is_secret(kind):
        head, _, field = kind.partition("_")
        try:
            idx = int(head.removeprefix("user")) - 1
        except ValueError:
            return None
        if 0 <= idx < len(users):
            return getattr(users[idx], field, None)
    return None


def classify_literal(value: str, users) -> str | None:
    """If a literal typed value is actually a configured secret, return its kind (to redact)."""
    for i, u in enumerate(users, start=1):
        for field in _SECRET_FIELDS:
            if value and value == getattr(u, field, None):
                return f"user{i}_{field}"
    for kind, text in VALUE_KINDS.items():
        if value == text:
            return kind
    return None
