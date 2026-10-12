import io

import pandas as pd
import pytest

from app.portfolio import PortfolioError, clean_symbol, parse_portfolio


def test_template_csv():
    h, u = parse_portfolio("p.csv", b"symbol,quantity,avg_price\nRELIANCE,10,2450.5\nINFY,5,\n")
    assert h == [{"symbol": "INFY", "quantity": 5.0, "avg_price": None},
                 {"symbol": "RELIANCE", "quantity": 10.0, "avg_price": 2450.5}]
    assert u == []


def test_broker_xlsx_with_preamble_and_duplicates():
    rows = [["Client ID", "AB1234", None], [None, None, None],
            ["Symbol", "Quantity Available", "Average Price"],
            ["NSE:TCS", "3", "3,500.00"], ["TCS-EQ", "2", "4000"], ["HDFCBANK", "0", "1600"]]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, header=False, index=False)
    h, _ = parse_portfolio("holdings.xlsx", buf.getvalue())
    assert h == [{"symbol": "TCS", "quantity": 5.0, "avg_price": 3700.0}]


def test_company_names_resolved_by_isin_or_reported():
    data = (b"Stock Name,ISIN,Quantity,Average buy price\n"
            b"Reliance Industries,INE002A01018,4,2400\nMystery Corp,INE999Z99999,1,10\n")
    h, u = parse_portfolio("groww.csv", data, isin_resolver=lambda isin: "RELIANCE" if isin == "INE002A01018" else None)
    assert [x["symbol"] for x in h] == ["RELIANCE"]
    assert u == ["Mystery Corp"]


def test_unrecognised_file():
    with pytest.raises(PortfolioError):
        parse_portfolio("x.csv", b"foo,bar\n1,2\n")
    with pytest.raises(PortfolioError):
        parse_portfolio("x.csv", b"symbol,quantity\nRELIANCE,0\n")


def test_clean_symbol():
    assert clean_symbol(" bse:M&M ") == "M&M"
    assert clean_symbol("RELIANCE.NS") == "RELIANCE"
    assert clean_symbol("Tata Motors") is None
    assert clean_symbol("=cmd|' /C calc'!A0") is None


def test_blank_symbol_cell_is_not_a_holding():
    data = (b"Stock Name,ISIN,Symbol,Quantity\nReliance Industries,INE002A01018,RELIANCE,10\n"
            b"Mystery Ltd,INE999Z99999,,3\n")
    h, u = parse_portfolio("x.csv", data, isin_resolver=lambda isin: None)
    assert [x["symbol"] for x in h] == ["RELIANCE"]
    assert u == ["Mystery Ltd"]
