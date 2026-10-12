"""FastAPI app: participant API (session auth), admin API (token auth), scheduler and web UI."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hmac
import io
import logging
import os
import re

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import auth
from .config import IS_PRODUCTION, ROOT, AdminSettings, Horizon, Settings, Strategy
from .data.base import ProviderError
from .notify import AISensyClient, DailyJobs, scheduler_loop
from .portfolio import TEMPLATE_CSV, GrowwIsinResolver, PortfolioAdvisor, PortfolioError, parse_portfolio
from .profile import TradingProfile, apply_profile, split
from .expiries import ExpiryCalendar
from .tradetron import TradetronError, analyse as analyse_tradetron, load_export
from .service import BudgetExceeded, QuotaExceeded, RecommendationService, today_ist
from .store import Store

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9&_\-]{1,24}$")
STATIC = ROOT / "static"
MAX_BODY = 2_000_000

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                                "base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RecommendRequest(BaseModel):
    instrument: str
    horizon: Horizon
    force_refresh: bool = False


class LoginRequest(BaseModel):
    phone: str
    password: str


class InviteAccept(BaseModel):
    token: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class NotifyPref(BaseModel):
    opt_in: bool


class NewParticipant(BaseModel):
    name: str
    phone: str


class ParticipantPatch(BaseModel):
    active: bool | None = None
    name: str | None = None


def check_production_config() -> None:
    """Refuse to start in production with settings that would expose data."""
    if not IS_PRODUCTION:
        return
    token = os.environ.get("ADMIN_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("ADMIN_TOKEN must be set to a random string of at least 24 characters in production")
    if os.environ.get("COOKIE_SECURE") == "0":
        raise RuntimeError("COOKIE_SECURE=0 is not allowed in production")
    if not (os.environ.get("SR_DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")):
        log.warning("No volume attached: participants, portfolios and settings will be LOST on every redeploy. "
                    "Attach a Railway volume (e.g. mounted at /data).")


def create_app(settings: Settings | None = None, service: RecommendationService | None = None,
               jobs: DailyJobs | None = None, start_scheduler: bool | None = None) -> FastAPI:
    check_production_config()
    settings = settings or Settings()
    service = service or RecommendationService(settings, Store(settings.data_dir / "app.db"))
    store = service.store
    if service.calendar is None and os.environ.get("EXPIRY_CALENDAR", "1") != "0":
        service.calendar = ExpiryCalendar(settings)
    advisor = jobs.advisor if jobs else PortfolioAdvisor(service)
    jobs = jobs or DailyJobs(advisor, AISensyClient())
    isin_resolver = GrowwIsinResolver(settings.data_dir)
    if start_scheduler is None:
        start_scheduler = os.environ.get("SCHEDULER_ENABLED", "1") != "0"

    login_limiter = auth.RateLimiter(10, 15 * 60)     # failed logins per IP / per phone
    admin_limiter = auth.RateLimiter(20, 15 * 60)     # failed admin-token attempts per IP

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(scheduler_loop(jobs)) if start_scheduler else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Strategy Recommender", lifespan=lifespan,
                  docs_url=None if IS_PRODUCTION else "/docs", redoc_url=None,
                  openapi_url=None if IS_PRODUCTION else "/openapi.json")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        # CSRF: state-changing API calls must come from our own JS (custom header => no cross-site form posts,
        # and a cross-site fetch would need a CORS preflight that this app never grants).
        if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
                and request.headers.get("x-requested-with") != "fetch"):
            return JSONResponse({"detail": "Missing X-Requested-With header"}, status_code=403)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if auth.cookie_secure():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    # ---- auth dependencies ------------------------------------------------
    def require_admin(request: Request, x_admin_token: str = Header(default="")) -> None:
        if not os.environ.get("ADMIN_TOKEN"):
            raise HTTPException(503, "ADMIN_TOKEN is not configured on the server")
        if _is_admin(x_admin_token):
            return
        if not admin_limiter.hit(client_ip(request)):
            raise HTTPException(429, "Too many attempts. Try again later.")
        raise HTTPException(401, "invalid admin token")

    def current_user(request: Request) -> dict:
        token = request.cookies.get(auth.SESSION_COOKIE)
        user = store.session_user(token) if token else None
        if not user:
            raise HTTPException(401, "Please sign in")
        return user

    def set_session(response: Response, user_id: int) -> None:
        token = auth.new_token()
        store.create_session(user_id, token, auth.SESSION_TTL_S)
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL_S, httponly=True,
                            secure=auth.cookie_secure(), samesite="lax", path="/")

    def public_base(request: Request) -> str:
        if os.environ.get("PUBLIC_BASE_URL"):
            return os.environ["PUBLIC_BASE_URL"].rstrip("/")
        if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
            return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
        return str(request.base_url).rstrip("/")

    def issue_invite(request: Request, user_id: int) -> str:
        token = auth.new_token()
        store.create_invite(user_id, token, auth.INVITE_TTL_S)
        # Token in the URL fragment: never sent to the server in requests, logs or Referer headers.
        return f"{public_base(request)}/invite#{token}"

    # ---- health ---------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz():
        store.month_spend()  # touches the DB
        return {"ok": True}

    # ---- participant auth -------------------------------------------------
    @app.post("/api/auth/login")
    def login(req: LoginRequest, request: Request, response: Response):
        ip = client_ip(request)
        try:
            phone = auth.normalize_phone(req.phone)
        except ValueError:
            phone = "invalid"
        if not login_limiter.hit(f"ip:{ip}") or not login_limiter.hit(f"phone:{phone}"):
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        user = store.get_user_by_phone(phone) if phone != "invalid" else None
        ok = auth.verify_password(req.password, user["password_hash"] if user else None)
        if not (ok and user and user["active"]):
            raise HTTPException(401, "Wrong phone number or password")
        login_limiter.reset(f"phone:{phone}")
        store.update_user(user["id"], last_login=today_ist())
        set_session(response, user["id"])
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(auth.SESSION_COOKIE)
        if token:
            store.delete_session(token)
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/accept-invite")
    def accept_invite(req: InviteAccept, request: Request, response: Response):
        if not login_limiter.hit(f"invite:{client_ip(request)}"):
            raise HTTPException(429, "Too many attempts. Try again later.")
        try:
            auth.validate_password(req.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        user_id = store.consume_invite(req.token)
        if not user_id:
            raise HTTPException(400, "This invite link is invalid or has expired. Ask the admin for a new one.")
        store.update_user(user_id, password_hash=auth.hash_password(req.password))
        store.delete_user_sessions(user_id)
        set_session(response, user_id)
        return {"ok": True}

    # ---- participant: self ------------------------------------------------
    @app.get("/api/me")
    def me(user: dict = Depends(current_user)):
        portfolio = store.get_portfolio(user["id"])
        return {"name": user["name"], "phone": auth.mask_phone(user["phone"]),
                "notify_opt_in": bool(user["notify_opt_in"]),
                "notifications_live": settings.admin.notify_enabled, "notify_time": settings.admin.notify_time,
                "has_portfolio": bool(portfolio), "profile": store.get_profile(user["id"])}

    @app.put("/api/me/profile")
    def set_profile(profile: TradingProfile, user: dict = Depends(current_user)):
        store.set_profile(user["id"], profile.model_dump())
        return {"profile": profile.model_dump()}

    @app.put("/api/me/notifications")
    def set_notifications(pref: NotifyPref, user: dict = Depends(current_user)):
        store.update_user(user["id"], notify_opt_in=int(pref.opt_in))
        return {"notify_opt_in": pref.opt_in}

    @app.post("/api/me/password")
    def change_password(req: PasswordChange, response: Response, user: dict = Depends(current_user)):
        if not auth.verify_password(req.current_password, user["password_hash"]):
            raise HTTPException(401, "Current password is wrong")
        try:
            auth.validate_password(req.new_password)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        store.update_user(user["id"], password_hash=auth.hash_password(req.new_password))
        store.delete_user_sessions(user["id"])   # sign out other devices
        set_session(response, user["id"])
        return {"ok": True}

    # ---- participant: portfolio (always scoped to the session user) ---------
    @app.get("/api/portfolio")
    def get_portfolio(user: dict = Depends(current_user)):
        return {"portfolio": store.get_portfolio(user["id"])}

    @app.post("/api/portfolio")
    async def upload_portfolio(file: UploadFile = File(...), user: dict = Depends(current_user)):
        data = await file.read(1_000_001)
        if not store.take_quota(user["id"], today_ist(), "upload", settings.admin.per_user_daily_uploads):
            raise HTTPException(429, "Upload limit reached for today")
        try:
            holdings, unresolved = await asyncio.to_thread(parse_portfolio, file.filename or "", data, isin_resolver)
        except PortfolioError as exc:
            raise HTTPException(422, str(exc))
        source = re.sub(r"[^\w.\- ]", "", os.path.basename(file.filename or "upload"))[:80]
        store.save_portfolio(user["id"], holdings, unresolved, source)
        return {"portfolio": store.get_portfolio(user["id"])}

    @app.delete("/api/portfolio")
    def delete_portfolio(user: dict = Depends(current_user)):
        store.delete_portfolio(user["id"])
        return {"ok": True}

    @app.get("/api/portfolio/template.csv")
    def portfolio_template():
        return PlainTextResponse(TEMPLATE_CSV, media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="portfolio_template.csv"'})

    @app.get("/api/me/today")
    def my_today(user: dict = Depends(current_user)):
        try:
            return advisor.today(user["id"])
        except BudgetExceeded as exc:
            raise HTTPException(402, str(exc))
        except ProviderError:
            raise HTTPException(502, "Market data is unavailable right now. Please try again shortly.")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # ---- participant: market tool ---------------------------------------
    @app.get("/api/options")
    def options(user: dict = Depends(current_user)):
        return {
            "instruments": [{"key": i["key"], "label": i["label"]} for i in settings.instruments],
            "horizons": [{"key": "1d", "label": "1 Day"}, {"key": "1w", "label": "1 Week"},
                         {"key": "1m", "label": "1 Month"}],
            "strategies": [{"id": s.id, "name": s.name, "category": s.category, "horizons": s.horizons}
                           for s in settings.strategies.values()],
        }

    @app.post("/api/recommend")
    def recommend(req: RecommendRequest, request: Request, x_admin_token: str = Header(default="")):
        is_admin = _is_admin(x_admin_token)
        user = None if is_admin else current_user(request)
        if not SYMBOL_RE.match(req.instrument):
            raise HTTPException(422, "instrument must be a plain symbol like NIFTY50 or RELIANCE")
        try:
            # Bypassing the cache costs money, so only the admin may force a fresh analysis.
            payload = service.recommend(req.instrument, req.horizon, force_refresh=req.force_refresh and is_admin,
                                        quota_user=None if is_admin else user["id"])
            # The analysis is shared across participants; suitability is applied per person on top.
            raw = store.get_profile(user["id"]) if user else None
            if raw:
                strategies = settings.strategies_for(req.horizon)
                eligible, excluded = split(strategies, TradingProfile(**raw))
                payload = {**payload, "recommendation": apply_profile(
                    payload["recommendation"], {s.id for s in eligible}, excluded, {s.id: s.name for s in strategies})}
            return payload
        except BudgetExceeded as exc:
            raise HTTPException(402, str(exc))
        except QuotaExceeded as exc:
            raise HTTPException(429, str(exc))
        except ProviderError:
            raise HTTPException(502, "Market data is unavailable for that instrument right now.")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    # ---- admin: settings & usage ----------------------------------------
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
    def put_settings(changes: dict = Body(...)):
        changes = {k: v for k, v in changes.items() if k in AdminSettings.model_fields}
        try:
            new = settings.update_admin(changes)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, str(exc))
        if {"active_model", "effort", "portfolio_benchmark"} & changes.keys():
            store.cache_clear()
        return {"settings": new.model_dump()}

    @app.get("/api/admin/usage", dependencies=[Depends(require_admin)])
    def usage():
        summary = store.usage_summary()
        summary["budget_usd"] = settings.admin.monthly_budget_usd
        return summary

    # ---- admin: strategies ------------------------------------------------
    @app.get("/api/admin/strategies", dependencies=[Depends(require_admin)])
    def list_strategies():
        return {"strategies": [st.model_dump() for st in settings.strategies.values()]}

    @app.post("/api/admin/strategies", dependencies=[Depends(require_admin)], status_code=201)
    def create_strategy(strategy: Strategy):
        try:
            saved = settings.save_strategy(strategy)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        store.cache_clear()
        return {"strategy": saved.model_dump()}

    @app.put("/api/admin/strategies/{strategy_id}", dependencies=[Depends(require_admin)])
    def update_strategy(strategy_id: str, strategy: Strategy):
        try:
            saved = settings.save_strategy(strategy, original_id=strategy_id)
        except KeyError:
            raise HTTPException(404, f"Unknown strategy {strategy_id!r}")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        store.cache_clear()
        return {"strategy": saved.model_dump()}

    @app.delete("/api/admin/strategies/{strategy_id}", dependencies=[Depends(require_admin)])
    def delete_strategy(strategy_id: str):
        try:
            settings.delete_strategy(strategy_id)
        except KeyError:
            raise HTTPException(404, f"Unknown strategy {strategy_id!r}")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        store.cache_clear()
        return {"deleted": strategy_id}

    @app.post("/api/admin/strategies/import", dependencies=[Depends(require_admin)])
    async def import_strategy(file: UploadFile = File(...)):
        """Turn a Tradetron export into a draft for the admin to review; nothing is saved here."""
        raw = await file.read(2_000_001)
        try:
            data, sha = load_export(raw)
            result = analyse_tradetron(data)
        except TradetronError as exc:
            raise HTTPException(422, str(exc))
        imports = settings.data_dir / "imports"
        imports.mkdir(exist_ok=True)
        (imports / f"{sha}.json").write_bytes(raw)          # keep the original for audit / re-translation
        draft = result["draft"]
        draft["source"] = {"format": data.get("_format"), "original_name": data.get("name"), "sha256": sha,
                           "imported": today_ist()}
        existing = next((st for st in settings.strategies.values()
                         if (st.source or {}).get("original_name") == data.get("name") or st.id == draft["id"]), None)
        warnings = result["warnings"]
        if existing:
            kept = ("description", "ideal_conditions", "avoid_conditions", "risk_notes", "risk_level",
                    "min_experience", "horizons", "category")
            draft = {**draft, **{k: getattr(existing, k) for k in kept}, "id": existing.id}
            draft["ideal_conditions"] = existing.ideal_conditions.model_dump()
            warnings = [w for w in warnings if not w.startswith("Ideal market conditions")]
            warnings.insert(0, f"Updates the existing strategy '{existing.name}': the logic, underlyings and capital "
                               "come from this file; your reviewed conditions and suitability are kept.")
        try:
            Strategy(**draft)
        except ValidationError as exc:
            raise HTTPException(422, f"Imported strategy is invalid: {exc}")
        return {"draft": draft, "warnings": warnings, "existing_id": existing.id if existing else None,
                "file": file.filename}

    @app.post("/api/admin/strategies/reload", dependencies=[Depends(require_admin)])
    def reload_strategies():
        try:
            loaded = settings.reload_strategies()
        except (ValueError, ValidationError, OSError) as exc:
            raise HTTPException(422, f"Strategy files invalid: {exc}")
        store.cache_clear()
        return {"loaded": sorted(loaded)}

    # ---- admin: participants ----------------------------------------------
    # The admin sees who has a portfolio and how many holdings, never the holdings themselves.
    @app.get("/api/admin/participants", dependencies=[Depends(require_admin)])
    def list_participants():
        return {"participants": store.list_users()}

    def _create_participant(request: Request, name: str, phone: str) -> dict:
        name = re.sub(r"\s+", " ", name).strip()[:80]
        if not name:
            raise ValueError("Name is required")
        phone = auth.normalize_phone(phone)
        user_id = store.create_user(name, phone)
        return {"id": user_id, "name": name, "phone": phone, "invite_url": issue_invite(request, user_id)}

    @app.post("/api/admin/participants", dependencies=[Depends(require_admin)], status_code=201)
    def add_participant(p: NewParticipant, request: Request):
        try:
            return _create_participant(request, p.name, p.phone)
        except ValueError as exc:
            raise HTTPException(409 if "exists" in str(exc) else 422, str(exc))

    @app.post("/api/admin/participants/import", dependencies=[Depends(require_admin)])
    def import_participants(request: Request, csv_text: str = Body(..., embed=True)):
        created, errors = [], []
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        if not reader.fieldnames or not {"name", "phone"} <= {f.strip().lower() for f in reader.fieldnames}:
            raise HTTPException(422, "CSV needs a header row with 'name' and 'phone' columns")
        for line, row in enumerate(reader, start=2):
            if line > 1001:
                errors.append({"line": line, "error": "Stopped: max 1000 participants per import"})
                break
            row = {k.strip().lower(): (v or "") for k, v in row.items() if k}
            try:
                created.append(_create_participant(request, row["name"], row["phone"]))
            except ValueError as exc:
                errors.append({"line": line, "error": str(exc)})
        return {"created": created, "errors": errors}

    @app.post("/api/admin/participants/{user_id}/invite", dependencies=[Depends(require_admin)])
    def reinvite(user_id: int, request: Request):
        if not store.get_user(user_id):
            raise HTTPException(404, "Unknown participant")
        store.update_user(user_id, password_hash=None)
        store.delete_user_sessions(user_id)
        return {"invite_url": issue_invite(request, user_id)}

    @app.patch("/api/admin/participants/{user_id}", dependencies=[Depends(require_admin)])
    def patch_participant(user_id: int, patch: ParticipantPatch):
        if not store.get_user(user_id):
            raise HTTPException(404, "Unknown participant")
        fields = {}
        if patch.active is not None:
            fields["active"] = int(patch.active)
            if not patch.active:
                store.delete_user_sessions(user_id)
        if patch.name:
            fields["name"] = patch.name.strip()[:80]
        store.update_user(user_id, **fields)
        return {"ok": True}

    @app.delete("/api/admin/participants/{user_id}", dependencies=[Depends(require_admin)])
    def delete_participant(user_id: int):
        if not store.get_user(user_id):
            raise HTTPException(404, "Unknown participant")
        store.delete_user(user_id)   # cascades to sessions, portfolio, recommendations, logs
        return {"deleted": user_id}

    # ---- admin: expiry calendar --------------------------------------------
    @app.get("/api/admin/expiries", dependencies=[Depends(require_admin)])
    def expiries():
        if service.calendar is None:
            return {"groww_configured": False, "underlyings": [], "disabled": True}
        return service.calendar.status()

    @app.post("/api/admin/expiries/refresh", dependencies=[Depends(require_admin)])
    def refresh_expiries():
        if service.calendar is None:
            raise HTTPException(409, "Expiry calendar is disabled")
        result = service.calendar.refresh()
        store.cache_clear()
        return result

    # ---- admin: notifications --------------------------------------------
    @app.get("/api/admin/notifications", dependencies=[Depends(require_admin)])
    def notifications():
        a = settings.admin
        return {"aisensy_configured": jobs.aisensy.configured, "enabled": a.notify_enabled,
                "notify_time": a.notify_time, "prepare_lead_minutes": a.prepare_lead_minutes,
                "weekdays_only": a.notify_weekdays_only, "holidays": a.market_holidays,
                "campaign": a.aisensy_campaign, "opted_in": len(store.opted_in_users()),
                "recent": store.recent_notifications(), "jobs": store.recent_jobs()}

    @app.post("/api/admin/notifications/test", dependencies=[Depends(require_admin)])
    def notification_test(phone: str = Body(..., embed=True)):
        try:
            phone = auth.normalize_phone(phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        ok, detail = jobs.send_test(phone)
        return {"ok": ok, "detail": detail}

    @app.post("/api/admin/jobs/{kind}/run", dependencies=[Depends(require_admin)], status_code=202)
    def run_job(kind: str, background: BackgroundTasks):
        if kind not in ("prepare", "send"):
            raise HTTPException(404, "Unknown job")
        background.add_task(jobs.run, kind)   # send skips anyone already notified today
        return {"started": kind}

    # ---- web UI ---------------------------------------------------------
    pages = {"/": "index.html", "/login": "login.html", "/invite": "invite.html", "/admin": "admin.html"}
    for route, page in pages.items():
        app.add_api_route(route, _page(page), include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _page(name: str):
    def handler():
        return FileResponse(STATIC / name)
    return handler


def _is_admin(token: str) -> bool:
    expected = os.environ.get("ADMIN_TOKEN", "")
    return bool(expected) and bool(token) and hmac.compare_digest(token.encode(), expected.encode())


def _safe_engine(service: RecommendationService) -> dict:
    try:
        return service.resolve_engine()
    except BudgetExceeded as exc:
        return {"engine": "blocked", "model": None, "budget_exceeded": True,
                "month_spend_usd": round(service.store.month_spend(), 4), "note": str(exc)}


app = create_app() if os.environ.get("SR_NO_AUTOAPP") != "1" else None
