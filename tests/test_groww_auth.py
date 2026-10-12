import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.data import providers
from app.data.base import ProviderNotConfigured
from app.data.providers import GrowwProvider, totp_now

IST = ZoneInfo("Asia/Kolkata")
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()   # RFC 6238 test key


def test_totp_matches_rfc6238_vectors():
    assert totp_now(RFC_SECRET, at=59, digits=8) == "94287082"
    assert totp_now(RFC_SECRET, at=1111111109, digits=8) == "07081804"
    assert totp_now(RFC_SECRET.lower(), at=59) == "287082"          # 6 digits, case-insensitive
    assert totp_now(RFC_SECRET.rstrip("="), at=59) == "287082"      # padding optional


class Resp:
    def __init__(self, token, ok=True):
        self.ok, self.status_code, self._t = ok, 200 if ok else 400, token

    def json(self):
        return {"token": self._t} if self.ok else {"error": {"displayMessage": "Invalid TOTP"}}


@pytest.fixture
def env_totp(monkeypatch):
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROWW_API_KEY", "api-key")
    monkeypatch.setenv("GROWW_TOTP_SECRET", RFC_SECRET)


def test_totp_login_request(env_totp, monkeypatch):
    sent = []
    monkeypatch.setattr(providers.requests, "post",
                        lambda url, headers, json, timeout: sent.append((url, headers, json)) or Resp("tok-1"))
    g = GrowwProvider()
    assert g._get_token() == "tok-1" and g._get_token() == "tok-1"
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url.endswith("/token/api/access") and headers["Authorization"] == "Bearer api-key"
    assert body["key_type"] == "totp" and len(body["totp"]) == 6 and body["totp"].isdigit()


def test_relogin_after_6am_reset(env_totp, monkeypatch):
    tokens = iter(["tok-day1", "tok-day2"])
    monkeypatch.setattr(providers.requests, "post", lambda url, headers, json, timeout: Resp(next(tokens)))
    now = {"t": datetime(2026, 10, 12, 5, 40, tzinfo=IST)}
    monkeypatch.setattr(providers, "now_ist", lambda: now["t"])
    g = GrowwProvider()
    assert g._get_token() == "tok-day1"
    now["t"] = datetime(2026, 10, 12, 5, 59, tzinfo=IST)
    assert g._get_token() == "tok-day1"                       # before the reset: reuse
    now["t"] = datetime(2026, 10, 12, 6, 1, tzinfo=IST)
    assert g._get_token() == "tok-day2"                       # after 06:00 IST: new login


def test_login_error_is_reported(env_totp, monkeypatch):
    monkeypatch.setattr(providers.requests, "post", lambda url, headers, json, timeout: Resp(None, ok=False))
    with pytest.raises(providers.ProviderError, match="Invalid TOTP"):
        GrowwProvider()._get_token()


def test_not_configured(monkeypatch):
    for k in ("GROWW_ACCESS_TOKEN", "GROWW_API_SECRET", "GROWW_TOTP_SECRET", "GROWW_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert not GrowwProvider.can_mint()
    with pytest.raises(ProviderNotConfigured):
        GrowwProvider()._get_token()
