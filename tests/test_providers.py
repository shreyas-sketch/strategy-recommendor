import pandas as pd

from app.data.providers import parse_candle_rows


def test_groww_rows_epoch_seconds():
    df = parse_candle_rows([[1735700000, 1, 2, 0.5, 1.5, 100], [1735700900, 1.5, 2, 1, 1.8, 120]])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index[0] == pd.Timestamp(1735700000, unit="s", tz="UTC")


def test_groww_rows_iso():
    df = parse_candle_rows([["2025-01-01T09:15:00", 1, 2, 0.5, 1.5, 100, 0]])
    assert df["close"].iloc[0] == 1.5
