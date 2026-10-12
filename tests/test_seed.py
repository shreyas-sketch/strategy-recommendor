import shutil
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_seed_strategies_load(tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    s = Settings(config_dir=cfg, data_dir=tmp_path / "data")
    assert set(s.strategies) == {"bnf_15_min_super_trend_intraday_instant", "atm_roll_aggressive_diversified_kapoor",
                                 "kc_trend_ride_nifty_bank_option_selling"}
    assert all(st.source and st.underlyings for st in s.strategies.values())
