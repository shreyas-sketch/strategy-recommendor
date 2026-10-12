"""Settings, admin overrides and strategy catalog loading."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("SR_CONFIG_DIR", ROOT / "config"))
# On Railway, attach a volume: RAILWAY_VOLUME_MOUNT_PATH is set automatically and all
# state (database, admin settings, strategies) lives there and survives redeploys.
DATA_DIR = Path(os.environ.get("SR_DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
IS_PRODUCTION = bool(os.environ.get("RAILWAY_ENVIRONMENT")) or os.environ.get("APP_ENV") == "production"

log = logging.getLogger(__name__)

Horizon = Literal["1d", "1w", "1m"]
HORIZONS: tuple[Horizon, ...] = ("1d", "1w", "1m")
Effort = Literal["low", "medium", "high"]
BudgetAction = Literal["downgrade", "rules_only", "block"]

CHEAPEST_MODEL = "claude-haiku-5-5"


class ModelInfo(BaseModel):
    id: str
    label: str
    tagline: str = ""
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    server_fallback: bool = False

    def cost_usd(self, input_tokens: int, output_tokens: int,
                 cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
        # 5-minute cache writes bill at 1.25x the input rate.
        return (
            input_tokens * self.input_per_mtok
            + cache_write_tokens * self.input_per_mtok * 1.25
            + cache_read_tokens * self.cache_read_per_mtok
            + output_tokens * self.output_per_mtok
        ) / 1_000_000


class AdminSettings(BaseModel):
    active_model: str
    effort: Effort = "medium"
    monthly_budget_usd: float = Field(0, ge=0)
    on_budget_exceeded: BudgetAction = "downgrade"
    provider_order: list[str] = ["groww", "twelvedata", "yfinance"]
    cache_minutes: dict[str, int] = {"1d": 15, "1w": 60, "1m": 360}
    # Fresh (uncached) AI analyses one participant may trigger per day — caps cost abuse.
    per_user_daily_analyses: int = Field(20, ge=0)
    # Portfolio uploads per participant per day (each re-upload re-runs their daily analysis).
    per_user_daily_uploads: int = Field(5, ge=0)
    # Daily WhatsApp notification (AISensy)
    notify_enabled: bool = False
    notify_time: str = Field("06:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    prepare_lead_minutes: int = Field(25, ge=0, le=180)
    notify_weekdays_only: bool = True
    market_holidays: list[Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]] = []  # IST dates, no message
    aisensy_campaign: str = ""
    # Index used as the market backdrop for portfolio recommendations.
    portfolio_benchmark: str = "NIFTY50"


class Conditions(BaseModel):
    trend: list[str] = []
    trend_strength: list[str] = []
    volatility: list[str] = []
    squeeze: bool | Literal["any"] = "any"


Category = Literal["trend", "mean_reversion", "breakout", "volatility_selling", "volatility_buying", "other"]


class Strategy(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str = Field(min_length=1, max_length=80)
    category: Category = "other"
    instruments: list[str] = []
    horizons: list[Horizon] = Field(default=list(HORIZONS), min_length=1)
    description: str = ""
    logic: str = Field("", max_length=20000)
    ideal_conditions: Conditions = Conditions()
    avoid_conditions: list[str] = []
    risk_notes: str = ""
    # Suitability — participants whose profile doesn't meet these never get this strategy.
    risk_level: Literal["low", "medium", "high"] = "medium"
    min_capital_inr: int = Field(0, ge=0)
    min_experience: Literal["beginner", "intermediate", "advanced"] = "beginner"
    # Instrument keys (config/settings.yaml) this strategy trades, e.g. ["BANKNIFTY"]. Each strategy is
    # judged on the market regime of its own underlying(s). Empty = generic (judged on the analysed instrument).
    underlyings: list[Annotated[str, Field(pattern=r"^[A-Z0-9&_\-]{1,24}$")]] = []
    # Which days it actually enters trades (checked against the Groww expiry calendar of its underlyings).
    active_days: Literal["any", "expiry_day", "expiry_and_day_before"] = "any"
    # Where it came from, e.g. {"format": "tt-strategy/v2", "original_name": ..., "sha256": ...}
    source: dict | None = None


class Settings:
    """Static config from settings.yaml merged with admin overrides on disk."""

    def __init__(self, config_dir: Path = CONFIG_DIR, data_dir: Path = DATA_DIR):
        self.config_dir = Path(config_dir)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._admin_path = self.data_dir / "admin_settings.json"
        self._lock = threading.Lock()

        raw = yaml.safe_load((self.config_dir / "settings.yaml").read_text())
        self.models: dict[str, ModelInfo] = {
            mid: ModelInfo(id=mid, **m) for mid, m in raw["models"].items()
        }
        self.instruments: list[dict] = raw.get("instruments", [])
        self._defaults = raw.get("defaults", {})
        self.admin = self._load_admin()
        self.strategies: dict[str, Strategy] = {}
        self._strategy_paths: dict[str, Path] = {}
        self._seed_strategies()
        self.reload_strategies()

    # ---- admin settings -------------------------------------------------
    def _load_admin(self) -> AdminSettings:
        data = dict(self._defaults)
        if self._admin_path.exists():
            data.update(json.loads(self._admin_path.read_text()))
        settings = AdminSettings(**data)
        self._validate_admin(settings)
        return settings

    def _validate_admin(self, s: AdminSettings) -> None:
        if s.active_model not in self.models:
            raise ValueError(f"Unknown model {s.active_model!r}; choose one of {list(self.models)}")

    def update_admin(self, changes: dict) -> AdminSettings:
        with self._lock:
            merged = AdminSettings(**{**self.admin.model_dump(), **changes})
            self._validate_admin(merged)
            self._admin_path.write_text(json.dumps(merged.model_dump(), indent=2))
            self.admin = merged
            return merged

    # ---- strategies -----------------------------------------------------
    @property
    def strategy_dir(self) -> Path:
        # Lives in the data dir so strategies added from the admin panel survive redeploys.
        return self.data_dir / "strategies"

    def _seed_strategies(self) -> None:
        if self.strategy_dir.exists():
            return
        self.strategy_dir.mkdir(parents=True)
        for path in (self.config_dir / "strategies").glob("*.y*ml"):
            shutil.copy2(path, self.strategy_dir / path.name)
        log.info("seeded strategies into %s", self.strategy_dir)

    def reload_strategies(self) -> dict[str, Strategy]:
        found: dict[str, Strategy] = {}
        paths: dict[str, Path] = {}
        for path in sorted(self.strategy_dir.glob("*.y*ml")):
            if path.name.startswith("_"):
                continue
            strat = Strategy(**yaml.safe_load(path.read_text()))
            if strat.id in found:
                raise ValueError(f"Duplicate strategy id {strat.id!r} in {path.name}")
            found[strat.id] = strat
            paths[strat.id] = path
        if not found:
            raise ValueError(f"No strategies found in {self.strategy_dir}")
        self.strategies = found
        self._strategy_paths = paths
        return found

    def save_strategy(self, strategy: Strategy, original_id: str | None = None) -> Strategy:
        """Create (original_id=None) or update a strategy; writes config/strategies/<id>.yaml."""
        with self._lock:
            if original_id is not None and original_id not in self.strategies:
                raise KeyError(original_id)
            if strategy.id in self.strategies and strategy.id != original_id:
                raise ValueError(f"A strategy with id {strategy.id!r} already exists")
            old_path = self._strategy_paths.get(original_id) if original_id else None
            path = old_path if old_path and strategy.id == original_id else self.strategy_dir / f"{strategy.id}.yaml"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(yaml.safe_dump(strategy.model_dump(), sort_keys=False, allow_unicode=True, width=100))
            tmp.replace(path)
            if old_path and old_path != path:
                old_path.unlink(missing_ok=True)
            self.reload_strategies()
            return self.strategies[strategy.id]

    def delete_strategy(self, strategy_id: str) -> None:
        with self._lock:
            if strategy_id not in self.strategies:
                raise KeyError(strategy_id)
            if len(self.strategies) == 1:
                raise ValueError("Cannot delete the last strategy")
            self._strategy_paths[strategy_id].unlink()
            self.reload_strategies()

    def strategies_for(self, horizon: Horizon) -> list[Strategy]:
        return [s for s in self.strategies.values() if horizon in s.horizons]

    def catalog_hash(self) -> str:
        blob = json.dumps([s.model_dump() for s in self.strategies.values()], sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def instrument(self, key: str) -> dict:
        """Return a configured instrument, or derive one for a plain NSE equity symbol."""
        key = key.strip().upper()
        for inst in self.instruments:
            if inst["key"].upper() == key:
                return inst
        return {
            "key": key,
            "label": key,
            "yfinance": f"{key}.NS",
            "twelvedata": {"symbol": key, "exchange": "NSE"},
            "groww": {"exchange": "NSE", "segment": "CASH", "groww_symbol": f"NSE-{key}"},
        }
