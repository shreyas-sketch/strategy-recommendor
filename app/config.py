"""Settings, admin overrides and strategy catalog loading."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("SR_CONFIG_DIR", ROOT / "config"))
DATA_DIR = Path(os.environ.get("SR_DATA_DIR", ROOT / "data"))

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


class Conditions(BaseModel):
    trend: list[str] = []
    trend_strength: list[str] = []
    volatility: list[str] = []
    squeeze: bool | Literal["any"] = "any"


class Strategy(BaseModel):
    id: str
    name: str
    category: str = "other"
    instruments: list[str] = []
    horizons: list[Horizon] = list(HORIZONS)
    description: str = ""
    logic: str = ""
    ideal_conditions: Conditions = Conditions()
    avoid_conditions: list[str] = []
    risk_notes: str = ""


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
    def reload_strategies(self) -> dict[str, Strategy]:
        found: dict[str, Strategy] = {}
        for path in sorted((self.config_dir / "strategies").glob("*.y*ml")):
            if path.name.startswith("_"):
                continue
            strat = Strategy(**yaml.safe_load(path.read_text()))
            if strat.id in found:
                raise ValueError(f"Duplicate strategy id {strat.id!r} in {path.name}")
            found[strat.id] = strat
        if not found:
            raise ValueError(f"No strategies found in {self.config_dir / 'strategies'}")
        self.strategies = found
        return found

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
