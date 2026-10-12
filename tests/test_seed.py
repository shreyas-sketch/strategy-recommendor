import json
import shutil
from pathlib import Path

import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = {"bnf_15_min_super_trend_intraday_instant", "atm_roll_aggressive_diversified_kapoor",
           "kc_trend_ride_nifty_bank_option_selling", "banknifty_supertrend_intraday_5_min",
           "kc_trend_ride_nifty_50_option_buying", "kc_trend_ride_nifty_50_option_selling",
           "nifty_supertrend_intraday_5_min_selling", "nifty_3_min_st_intraday",
           "kc_trend_ride_nifty_bank_option_buying", "banknifty_3_min_st_intraday_sl"}


def cfg(tmp_path):
    c = tmp_path / "config"
    shutil.copytree(ROOT / "config", c)
    return c


def test_shipped_seed_strategies_load(tmp_path):
    s = Settings(config_dir=cfg(tmp_path), data_dir=tmp_path / "data")
    assert set(s.strategies) == SHIPPED
    assert all(st.source and st.underlyings for st in s.strategies.values())


def test_new_shipped_strategy_reaches_existing_volume(tmp_path):
    """A volume seeded before the manifest existed gets only the new file; nothing is overwritten."""
    c = cfg(tmp_path)
    data = tmp_path / "data"
    (data / "strategies").mkdir(parents=True)
    for name in ("bnf_15_min_super_trend_intraday_instant", "atm_roll_aggressive_diversified_kapoor",
                 "kc_trend_ride_nifty_bank_option_selling"):
        doc = yaml.safe_load((c / "strategies" / f"{name}.yaml").read_text())
        doc["risk_notes"] = "EDITED."
        (data / "strategies" / f"{name}.yaml").write_text(yaml.safe_dump(doc))   # admin-edited copies
    s = Settings(config_dir=c, data_dir=data)
    assert set(s.strategies) == SHIPPED
    assert s.strategies["kc_trend_ride_nifty_bank_option_selling"].risk_notes == "EDITED."   # edits kept


def test_deleted_shipped_strategy_stays_deleted(tmp_path):
    c = cfg(tmp_path)
    s = Settings(config_dir=c, data_dir=tmp_path / "data")
    s.delete_strategy("kc_trend_ride_nifty_bank_option_selling")
    s2 = Settings(config_dir=c, data_dir=tmp_path / "data")               # restart / redeploy
    assert "kc_trend_ride_nifty_bank_option_selling" not in s2.strategies
    seeded = json.loads((tmp_path / "data" / "strategies" / ".seeded.json").read_text())
    assert "kc_trend_ride_nifty_bank_option_selling.yaml" in seeded


def test_seed_skips_id_already_present_under_other_file(tmp_path):
    c = cfg(tmp_path)
    data = tmp_path / "data"
    (data / "strategies").mkdir(parents=True)
    text = (c / "strategies" / "banknifty_supertrend_intraday_5_min.yaml").read_text()
    (data / "strategies" / "my_copy.yaml").write_text(text)               # admin imported it via the UI
    s = Settings(config_dir=c, data_dir=data)                             # must not raise duplicate-id
    assert "banknifty_supertrend_intraday_5_min" in s.strategies
