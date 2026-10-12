"""Google Sign-In (OpenID Connect authorization-code flow), server-side.

Only pre-approved emails get in: the app looks the verified Google email up among
participants (or admins); unknown emails are refused. No Google tokens are stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
STATE_COOKIE = "sr_oauth"
STATE_TTL_S = 600
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OAuthError(ValueError):
    pass


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ValueError("Enter a valid email address")
    return email


def _secret() -> bytes:
    # Signs the short-lived state cookie. Derived from the OAuth client secret so no extra variable is needed.
    return hashlib.sha256(b"sr-oauth-state|" + os.environ.get("GOOGLE_CLIENT_SECRET", "").encode()).digest()


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return f"{body}.{sig}"


def _unsign(value: str) -> dict:
    try:
        body, sig = value.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(sig, expected):
            raise OAuthError("bad state signature")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError) as exc:
        raise OAuthError("invalid sign-in state") from exc
    if payload.get("exp", 0) < time.time():
        raise OAuthError("sign-in took too long — please try again")
    return payload


def start(redirect_uri: str, role: str) -> tuple[str, str]:
    """Return (Google URL to redirect to, signed state cookie value)."""
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = AUTH_URL + "?" + urlencode({
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": redirect_uri, "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
    })
    return url, _sign({"state": state, "nonce": nonce, "role": role, "exp": int(time.time()) + STATE_TTL_S})


def _jwt_claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise OAuthError("malformed ID token") from exc


def finish(code: str, state: str, cookie: str | None, redirect_uri: str) -> tuple[str, str, str]:
    """Validate the callback and return (verified email, name, role requested at start)."""
    if not cookie:
        raise OAuthError("sign-in session expired — please try again")
    saved = _unsign(cookie)
    if not state or not hmac.compare_digest(state, saved["state"]):
        raise OAuthError("sign-in state mismatch — please try again")
    try:
        resp = requests.post(TOKEN_URL, data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"], "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"}, timeout=15)
    except requests.RequestException as exc:
        raise OAuthError("could not reach Google") from exc
    if not resp.ok:
        raise OAuthError("Google rejected the sign-in")
    # The ID token comes straight from Google's token endpoint over TLS, so per OpenID Connect
    # Core 3.1.3.7 the TLS connection authenticates it; we still check every claim.
    claims = _jwt_claims(resp.json().get("id_token", ""))
    if claims.get("aud") != os.environ["GOOGLE_CLIENT_ID"] or claims.get("iss") not in ISSUERS:
        raise OAuthError("ID token was not issued for this app")
    if claims.get("exp", 0) < time.time():
        raise OAuthError("ID token expired")
    if not hmac.compare_digest(str(claims.get("nonce", "")), saved["nonce"]):
        raise OAuthError("ID token nonce mismatch")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise OAuthError("your Google email is not verified")
    return claims["email"].strip().lower(), claims.get("name") or "", saved["role"]
