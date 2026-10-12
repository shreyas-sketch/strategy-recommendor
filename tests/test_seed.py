import hashlib
import json
import subprocess
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


def test_shipped_history_covers_every_shipped_file():
    """Run tools/update_shipped_history.py after editing a shipped strategy."""
    history = json.loads((ROOT / "config" / "strategies" / ".shipped_history.json").read_text())
    for path in (ROOT / "config" / "strategies").glob("*.yaml"):
        if not path.name.startswith("_"):
            assert hashlib.sha256(path.read_bytes()).hexdigest() in history[path.name], path.name


def _old_version(name, commit="63bf2b7"):
    return subprocess.run(["git", "show", f"{commit}:config/strategies/{name}"], cwd=ROOT,
                          capture_output=True, check=True).stdout


def test_unedited_shipped_copy_is_updated_edited_copy_kept(tmp_path):
    """KC strategies shipped before their signal logic was known get it on the next deploy, unless edited."""
    c = cfg(tmp_path)
    data = tmp_path / "data"
    strategies = data / "strategies"
    strategies.mkdir(parents=True)
    names = [p.name for p in (c / "strategies").glob("*.yaml")]
    for name in names:
        shutil.copy2(c / "strategies" / name, strategies / name)
    (strategies / ".seeded.json").write_text(json.dumps(names))
    (strategies / "kc_trend_ride_nifty_50_option_buying.yaml").write_bytes(
        _old_version("kc_trend_ride_nifty_50_option_buying.yaml"))                     # untouched old copy
    edited = yaml.safe_load(_old_version("kc_trend_ride_nifty_50_option_selling.yaml"))
    edited["risk_notes"] = "MY NOTES"
    (strategies / "kc_trend_ride_nifty_50_option_selling.yaml").write_text(yaml.safe_dump(edited))
    s = Settings(config_dir=c, data_dir=data)
    assert "Renko" in s.strategies["kc_trend_ride_nifty_50_option_buying"].logic
    assert s.strategies["kc_trend_ride_nifty_50_option_selling"].risk_notes == "MY NOTES"
    assert "Renko" not in s.strategies["kc_trend_ride_nifty_50_option_selling"].logic


def test_kc_strategies_carry_signal_logic(tmp_path):
    s = Settings(config_dir=cfg(tmp_path), data_dir=tmp_path / "data")
    kc = [st for sid, st in s.strategies.items() if sid.startswith("kc_trend_ride")]
    assert len(kc) == 4
    for st in kc:
        assert "Renko" in st.logic and "9 EMA" in st.logic and "Supertrend(ATR 10, multiplier 3)" in st.logic
        assert "set by an external process (logic not included" not in st.logic
        box = "10 points" if "NIFTY 50" in st.logic.split("Chart:")[1][:40] else "30 points"
        assert f"box size {box}" in st.logic

