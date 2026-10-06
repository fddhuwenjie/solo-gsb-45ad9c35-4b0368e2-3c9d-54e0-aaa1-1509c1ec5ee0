"""Security helpers: opaque bearer tokens, hashed at rest."""
from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32
ROLES = ("reviewer1", "reviewer2", "arbiter")


def generate_token() -> str:
    """Opaque credential shown exactly once, in the create response."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
