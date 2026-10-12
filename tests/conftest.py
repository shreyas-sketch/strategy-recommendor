import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

os.environ["SR_NO_AUTOAPP"] = "1"
os.environ["COOKIE_SECURE"] = "0"      # TestClient talks plain http
os.environ.pop("RAILWAY_ENVIRONMENT", None)

from app.config import Settings  # noqa: E402
from app.data.base import Provider, normalize  # noqa: E402
from app.data.providers import DataRouter  # noqa: E402
from app.llm.recommender import DailyRecommendation, Recommendation  # noqa: E402
from app.service import RecommendationService  # noqa: E402
from app.store import Store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def make_candles(kind: str, n: int = 300, freq: str = "1D", seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    if kind == "trend_up":
        rets = 0.004 + rng.normal(0, 0.004, n)
    elif kind == "range":
        # strongly mean-reverting around 100
        price, closes = 100.0, []
        for _ in range(n):
            price += 0.5 * (100 - price) + rng.normal(0, 0.6)
            closes.append(price)
        closes = np.array(closes)
        rets = None
    elif kind == "volatile":
        rets = rng.normal(0, 0.004, n)
        rets[-40:] = rng.normal(0, 0.03, 40)
    else:
        raise ValueError(kind)
    if rets is not None:
        closes = 100 * np.exp(np.cumsum(rets))
    opens = np.r_[closes[0], closes[:-1]]
    spread = np.abs(rng.normal(0, 0.003, n)) * closes + np.abs(closes - opens) * 0.2
    df = pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + spread, "low": np.minimum(opens, closes) - spread,
        "close": closes, "volume": rng.integers(1_000, 5_000, n).astype(float),
    }, index=pd.date_range("2025-01-01", periods=n, freq=freq, tz="Asia/Kolkata"))
    return normalize(df)


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, kind="trend_up"):
        self.kind = kind
        self.calls = 0

    def fetch(self, instrument, spec):
        self.calls += 1
        freq = {"15m": "15min", "1h": "1h", "1d": "1D"}[spec.interval]
        return make_candles(self.kind, freq=freq)


class FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def parse(self, **kwargs):
        self.parent.calls.append(kwargs)
        ids = [s["id"] for s in self.parent.catalog_from(kwargs)]
        cls = kwargs["output_format"]
        extra = {}
        if cls is DailyRecommendation:
            extra = dict(run_on="NIFTY 50", why="The index trends strongly.",
                         whatsapp_line="NIFTY is\ntrending up strongly.")
        else:
            extra = dict(regime="strong up")
        rec = cls(**extra,
            market_summary="Strong uptrend.", confidence="high",
            top_strategy_id=ids[0],
            why_top_over_others=f"{ids[0]} beats {ids[-1]} because ADX 31 favours trend following.",
            deciding_factors=[{"factor": "ADX 31 — strong trend", "favors": [ids[0], "made_up"], "hurts": ids[1:]}],
            rankings=[{"strategy_id": i, "fit_score": 90 - 10 * k, "verdict": "recommended",
                       "why": "x", "watch_out": "y", "vs_top": f"loses to {ids[0]}"} for k, i in enumerate(ids)] +
                     [{"strategy_id": "made_up", "fit_score": 99, "verdict": "recommended", "why": "",
                       "watch_out": "", "vs_top": ""}],
            **({"reassess_if": ["ADX drops below 20"]} if cls is Recommendation else {}),
        )
        usage = SimpleNamespace(input_tokens=3000, output_tokens=1000,
                                cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(stop_reason=self.parent.stop_reason, parsed_output=rec, usage=usage,
                               model=kwargs["model"], stop_details=None, _request_id="req_test")


class FakeAnthropic:
    def __init__(self, stop_reason="end_turn"):
        import json, re
        self.calls = []
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=FakeMessages(self))
        self._json, self._re = json, re

    def catalog_from(self, kwargs):
        text = kwargs["system"][0]["text"]
        return self._json.loads(text.split("Strategy catalog:\n", 1)[1])


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    # Tests run against the generic example strategies; Tradetron tests import the real ones on top.
    for f in (cfg / "strategies").glob("*.yaml"):
        if not f.name.startswith("_"):
            f.unlink()
    for f in (cfg / "strategies" / "examples").glob("*.yaml"):
        shutil.copy2(f, cfg / "strategies" / f.name)
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    settings = Settings(config_dir=cfg, data_dir=tmp_path / "data")
    provider = FakeProvider()
    client = FakeAnthropic()
    from app.llm.recommender import ClaudeRecommender
    service = RecommendationService(settings, Store(settings.data_dir / "t.db"),
                                    router=DataRouter({"fake": provider}),
                                    recommender=ClaudeRecommender(client))
    settings.update_admin({"provider_order": ["fake"]})
    return SimpleNamespace(settings=settings, service=service, provider=provider, client=client)


@pytest.fixture
def web(env):
    """HTTP clients: admin(), and participant(name, phone) already signed in."""
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import create_app
    from app.notify import DailyJobs
    from app.daily import DailyAdvisor

    class FakeAISensy:
        configured = True

        def __init__(self):
            self.sent = []

        def send(self, **kw):
            self.sent.append(kw)
            return True, "HTTP 200"

    aisensy = FakeAISensy()
    jobs = DailyJobs(DailyAdvisor(env.service), aisensy)
    app = create_app(env.settings, env.service, jobs=jobs, start_scheduler=False)

    def new_client(**headers):
        return TestClient(app, headers={"X-Requested-With": "fetch", **headers})

    def participant(name="Asha Rao", phone="9876543210", password="correct-horse-1"):
        store = env.service.store
        uid = store.create_user(name, auth.normalize_phone(phone))
        store.update_user(uid, password_hash=auth.hash_password(password))
        c = new_client()
        r = c.post("/api/auth/login", json={"phone": phone, "password": password})
        assert r.status_code == 200, r.text
        c.user_id = uid
        return c

    return SimpleNamespace(app=app, jobs=jobs, aisensy=aisensy, client=new_client,
                           admin=lambda: new_client(**{"X-Admin-Token": "secret"}), participant=participant)
