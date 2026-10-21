"""Admin-only HTTP routes for the research module (/api/admin/research/*)."""

from __future__ import annotations

from fastapi import Body, Depends, File, Form, HTTPException, Response, UploadFile

from ..data.base import ProviderError
from .ingest import IngestError
from .service import MARKET_SYMBOLS, DuplicateImport, ResearchService, clean

MAX_UPLOAD = 60_000_000      # a year and a half of 1-minute bars with indicators is ~25 MB
PREFIX = "/api/admin/research"
LARGE_BODY_PATHS = (f"{PREFIX}/import", f"{PREFIX}/restore")


async def _read(file: UploadFile, limit: int = MAX_UPLOAD) -> bytes:
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, f"File is larger than {limit // 1_000_000} MB")
    return raw


def register(app, research: ResearchService, require_admin, require_master) -> None:
    admin = [Depends(require_admin)]

    @app.get(f"{PREFIX}/status", dependencies=admin)
    def status():
        return research.status()

    @app.get(f"{PREFIX}/imports", dependencies=admin)
    def imports():
        return clean({"imports": research.db.list_imports(), "market": research.db.market_summary(),
                      "symbols": list(MARKET_SYMBOLS)})

    @app.post(f"{PREFIX}/import")
    async def import_file(file: UploadFile = File(...), kind: str = Form(...), target: str = Form(""),
                          replace: bool = Form(False), principal: dict = Depends(require_admin)):
        raw = await _read(file)
        try:
            return research.import_file(raw, file.filename or "upload.csv", kind, target or None,
                                        principal.get("email", ""), replace=replace)
        except DuplicateImport as exc:
            raise HTTPException(409, str(exc))
        except IngestError as exc:
            raise HTTPException(422, str(exc))

    @app.post(f"{PREFIX}/fetch-daily")
    def fetch_daily(symbols: list[str] = Body(list(MARKET_SYMBOLS), embed=True), principal: dict = Depends(require_admin)):
        try:
            return clean({"results": research.fetch_daily([s.upper() for s in symbols], uploaded_by=principal.get("email", ""))})
        except ProviderError as exc:
            raise HTTPException(503, str(exc))

    @app.get(f"{PREFIX}/strategies", dependencies=admin)
    def strategies():
        return clean({"strategies": research.strategies_view(), "symbols": list(MARKET_SYMBOLS)})

    @app.post(f"{PREFIX}/strategies", dependencies=admin, status_code=201)
    def create_strategy(s: dict = Body(...)):
        try:
            return clean({"strategy": research.save_strategy(s, create=True)})
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.put(f"{PREFIX}/strategies/{{sid}}", dependencies=admin)
    def update_strategy(sid: str, s: dict = Body(...)):
        try:
            return clean({"strategy": research.save_strategy({**s, "id": sid})})
        except KeyError:
            raise HTTPException(404, "Unknown strategy")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.delete(f"{PREFIX}/strategies/{{sid}}", dependencies=admin)
    def delete_strategy(sid: str):
        try:
            research.delete_strategy(sid)
        except KeyError:
            raise HTTPException(404, "Unknown strategy")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"deleted": sid}

    @app.get(f"{PREFIX}/market", dependencies=admin)
    def market():
        return research.market_view()

    @app.get(f"{PREFIX}/performance", dependencies=admin)
    def performance(start: str | None = None, end: str | None = None):
        return research.performance(start, end)

    @app.get(f"{PREFIX}/relationships", dependencies=admin)
    def relationships():
        return research.relationships()

    @app.get(f"{PREFIX}/recommendation", dependencies=admin)
    def recommendation():
        return clean({"recommendation": research.latest_recommendation()})

    @app.post(f"{PREFIX}/recommendation/run", dependencies=admin)
    def run_recommendation():
        rec = research.recommend(save=True)
        research.evaluate_actuals()
        return {"recommendation": rec}

    @app.get(f"{PREFIX}/history", dependencies=admin)
    def history():
        return research.history()

    @app.get(f"{PREFIX}/backtest", dependencies=admin)
    def backtest():
        return research.backtest()

    @app.get(f"{PREFIX}/settings", dependencies=admin)
    def get_settings():
        return research.settings_view()

    @app.put(f"{PREFIX}/settings", dependencies=[Depends(require_master)])
    def put_settings(body: dict = Body(...)):
        try:
            return research.update_settings(body.get("engine"), body.get("config"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc))

    @app.get(f"{PREFIX}/backup", dependencies=[Depends(require_master)])
    def backup():
        from datetime import date
        return Response(research.backup(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="research-backup-{date.today()}.zip"'})

    @app.post(f"{PREFIX}/restore", dependencies=[Depends(require_master)])
    async def restore(file: UploadFile = File(...)):
        raw = await _read(file, limit=500_000_000)
        try:
            return research.restore(raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
