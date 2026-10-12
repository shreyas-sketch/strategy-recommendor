"""FastAPI app: participant API, admin API and the static web UI."""

from __future__ import annotations

import hmac
import logging
import os
import re

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .config import ROOT, BudgetAction, Effort, Horizon, Settings, Strategy
from .data.base import ProviderError
from .service import BudgetExceeded, RecommendationService
from .store import Store

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&_\-]{1,24}$")
STATIC = ROOT / "static"


class RecommendRequest(BaseModel):
    instrument: str
    horizon: Horizon
    force_refresh: bool = False


class AdminUpdate(BaseModel):
    active_model: str | None = None
    effort: Effort | None = None
    monthly_budget_usd: float | None = None
    on_budget_exceeded: BudgetAction | None = None
    provider_order: list[str] | None = None
    cache_minutes: dict[str, int] | None = None


def create_app(settings: Settings | None = None, service: RecommendationService | None = None) -> FastAPI:
    settings = settings or Settings()
    service = service or RecommendationService(settings, Store(settings.data_dir / "app.db"))
    app = FastAPI(title="Strategy Recommender")

    def require_admin(x_admin_token: str = Header(default="")) -> None:
        if not os.environ.get("ADMIN_TOKEN"):
            raise HTTPException(503, "ADMIN_TOKEN is not configured on the server")
        if not _is_admin(x_admin_token):
            raise HTTPException(401, "invalid admin token")

    # ---- participant API ------------------------------------------------
    @app.get("/api/options")
    def options():
        return {
            "instruments": [{"key": i["key"], "label": i["label"]} for i in settings.instruments],
            "horizons": [{"key": "1d", "label": "1 Day"}, {"key": "1w", "label": "1 Week"},
                         {"key": "1m", "label": "1 Month"}],
            "strategies": [{"id": s.id, "name": s.name, "category": s.category, "horizons": s.horizons}
                           for s in settings.strategies.values()],
        }

    @app.post("/api/recommend")
    def recommend(req: RecommendRequest, x_admin_token: str = Header(default="")):
        if not SYMBOL_RE.match(req.instrument):
            raise HTTPException(422, "instrument must be a plain symbol like NIFTY50 or RELIANCE")
        # Bypassing the cache costs money, so only the admin may force a fresh analysis.
        force = req.force_refresh and _is_admin(x_admin_token)
        try:
            return service.recommend(req.instrument, req.horizon, force_refresh=force)
        except BudgetExceeded as exc:
            raise HTTPException(402, str(exc))
        except ProviderError as exc:
            raise HTTPException(502, f"Market data unavailable: {exc}")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # ---- admin API ------------------------------------------------------
    @app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
    def get_settings():
        return {
            "settings": settings.admin.model_dump(),
            "models": [m.model_dump() for m in settings.models.values()],
            "engine_now": _safe_engine(service),
            "providers": list(service.router.providers),
            "strategies_loaded": len(settings.strategies),
        }

    @app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
    def put_settings(update: AdminUpdate):
        changes = update.model_dump(exclude_none=True)
        try:
            new = settings.update_admin(changes)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc))
        if {"active_model", "effort"} & changes.keys():
            service.store.cache_clear()
        return {"settings": new.model_dump()}

    @app.get("/api/admin/usage", dependencies=[Depends(require_admin)])
    def usage():
        summary = service.store.usage_summary()
        summary["budget_usd"] = settings.admin.monthly_budget_usd
        return summary

    @app.get("/api/admin/strategies", dependencies=[Depends(require_admin)])
    def list_strategies():
        return {"strategies": [st.model_dump() for st in settings.strategies.values()]}

    @app.post("/api/admin/strategies", dependencies=[Depends(require_admin)], status_code=201)
    def create_strategy(strategy: Strategy):
        try:
            saved = settings.save_strategy(strategy)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        service.store.cache_clear()
        return {"strategy": saved.model_dump()}

    @app.put("/api/admin/strategies/{strategy_id}", dependencies=[Depends(require_admin)])
    def update_strategy(strategy_id: str, strategy: Strategy):
        try:
            saved = settings.save_strategy(strategy, original_id=strategy_id)
        except KeyError:
            raise HTTPException(404, f"Unknown strategy {strategy_id!r}")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        service.store.cache_clear()
        return {"strategy": saved.model_dump()}

    @app.delete("/api/admin/strategies/{strategy_id}", dependencies=[Depends(require_admin)])
    def delete_strategy(strategy_id: str):
        try:
            settings.delete_strategy(strategy_id)
        except KeyError:
            raise HTTPException(404, f"Unknown strategy {strategy_id!r}")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        service.store.cache_clear()
        return {"deleted": strategy_id}

    @app.post("/api/admin/strategies/reload", dependencies=[Depends(require_admin)])
    def reload_strategies():
        try:
            loaded = settings.reload_strategies()
        except (ValueError, ValidationError, OSError) as exc:
            raise HTTPException(422, f"Strategy files invalid: {exc}")
        service.store.cache_clear()
        return {"loaded": sorted(loaded)}

    # ---- web UI ---------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "admin.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _is_admin(token: str) -> bool:
    expected = os.environ.get("ADMIN_TOKEN", "")
    return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())


def _safe_engine(service: RecommendationService) -> dict:
    try:
        return service.resolve_engine()
    except BudgetExceeded as exc:
        return {"engine": "blocked", "model": None, "budget_exceeded": True,
                "month_spend_usd": round(service.store.month_spend(), 4), "note": str(exc)}


app = create_app() if os.environ.get("SR_NO_AUTOAPP") != "1" else None
