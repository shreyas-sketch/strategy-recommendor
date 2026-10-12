"""Participant authentication: passwords, sessions, invites, rate limiting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque

SESSION_COOKIE = "sr_session"
SESSION_TTL_S = 14 * 86400
INVITE_TTL_S = 7 * 86400
MIN_PASSWORD = 8
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${dig}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), dig=base64.b64encode(digest).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Burn comparable time so "no password set" is not distinguishable by timing.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, dklen=32, **_SCRYPT)
        return False
    try:
        _, n, r, p, salt, dig = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                              n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, base64.b64decode(dig))


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > 128:
        raise ValueError("Password is too long")


_CODE_PEPPER = secrets.token_bytes(32)   # per-process: pending codes simply expire on restart


def code_digest(value: str) -> str:
    """Keyed hash for short-lived login codes (6-digit codes are too small for a plain hash)."""
    return hmac.new(_CODE_PEPPER, value.encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_phone(raw: str) -> str:
    """Return digits with country code (AISensy format), e.g. '9876543210' -> '919876543210'."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    if not 11 <= len(digits) <= 15:
        raise ValueError("Enter a valid mobile number with country code, e.g. 9198XXXXXXXX")
    return digits


def mask_phone(phone: str) -> str:
    return f"{phone[:2]}******{phone[-3:]}" if phone and len(phone) > 5 else "***"


class RateLimiter:
    """In-memory sliding window. Single-process app (Railway: 1 replica), so memory is enough."""

    def __init__(self, max_events: int, window_s: float):
        self.max_events = max_events
        self.window_s = window_s
        self._events: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record an event; return False when the key is over the limit."""
        now = time.monotonic()
        with self._lock:
            q = self._events[key]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.max_events:
                return False
            q.append(now)
            if len(self._events) > 10_000:  # bound memory
                for k in [k for k, v in self._events.items() if not v]:
                    del self._events[k]
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


def cookie_secure() -> bool:
    return os.environ.get("COOKIE_SECURE", "1") != "0"
