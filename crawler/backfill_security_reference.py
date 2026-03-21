#!/usr/bin/env python3
"""補齊 security_reference 表（股本、發行股數等）。"""

import duckdb
import datetime
import re
import requests
import time

DISP_DB = "/Users/slking/taiwan_stock_dashboard/處置股研究/disposition_research.duckdb"

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _safe_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _safe_float(val) -> float:
    try:
        # "新台幣                 10.0000元" → 10.0
        s = str(val).strip()
        m = re.search(r"([\d.]+)", s)
        return float(m.group(1)) if m else 0
    except (ValueError, TypeError):
        return 0


def _parse_date(s: str) -> datetime.date | None:
    """民國 YYYYMMDD → date。"""
    s = str(s).strip()
    if len(s) == 7:
        # 民國年 1140905
        y = int(s[:3]) + 1911
        return datetime.date(y, int(s[3:5]), int(s[5:7]))
    elif len(s) == 8:
        return datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def fetch_and_upsert():
    today = datetime.date.today()
    con = duckdb.connect(DISP_DB)

    # --- TWSE ---
    print("抓取 TWSE 公司基本資料...")
    r = requests.get(TWSE_URL, headers=HEADERS, timeout=30).json()
    twse_count = 0
    for row in r:
        code = row.get("公司代號", "").strip()
        if not code or len(code) != 4 or not code.isdigit():
            continue

        name = row.get("公司簡稱", "").strip()
        industry_code = row.get("產業別", "").strip()
        capital = _safe_int(row.get("實收資本額", 0))
        shares = _safe_int(row.get("已發行普通股數或TDR原股發行股數", 0))
        par = _safe_float(row.get("普通股每股面額", "10"))
        list_date = _parse_date(row.get("上市日期", ""))

        con.execute("""
            INSERT OR REPLACE INTO security_reference
            (as_of_date, exchange, symbol, name, market, industry_code, industry,
             sector_group, list_date, paid_in_capital, issued_common_shares, par_value,
             source_label, source_url, fetched_at)
            VALUES (?, 'TWSE', ?, ?, 'TWSE', ?, '', '', ?, ?, ?, ?,
                    'twse_opendata_t187ap03_L', ?, CURRENT_TIMESTAMP)
        """, [today, code, name, industry_code, list_date, capital, shares, par, TWSE_URL])
        twse_count += 1

    print(f"  TWSE: {twse_count} 筆")
    time.sleep(3)

    # --- TPEx ---
    print("抓取 TPEx 公司基本資料...")
    r2 = requests.get(TPEX_URL, headers=HEADERS, timeout=30).json()
    tpex_count = 0
    for row in r2:
        code = row.get("SecuritiesCompanyCode", "").strip()
        if not code or len(code) != 4 or not code.isdigit():
            continue

        name = row.get("CompanyAbbreviation", "").strip()
        industry_code = row.get("SecuritiesIndustryCode", "").strip()
        capital = _safe_int(row.get("Paidin.Capital.NTDollars", 0))
        shares = _safe_int(row.get("IssueShares", 0))
        par = _safe_float(row.get("ParValueOfCommonStock", "10"))
        list_date = _parse_date(row.get("DateOfListing", ""))

        con.execute("""
            INSERT OR REPLACE INTO security_reference
            (as_of_date, exchange, symbol, name, market, industry_code, industry,
             sector_group, list_date, paid_in_capital, issued_common_shares, par_value,
             source_label, source_url, fetched_at)
            VALUES (?, 'TPEx', ?, ?, 'TPEx', ?, '', '', ?, ?, ?, ?,
                    'tpex_opendata_t187ap03_O', ?, CURRENT_TIMESTAMP)
        """, [today, code, name, industry_code, list_date, capital, shares, par, TPEX_URL])
        tpex_count += 1

    print(f"  TPEx: {tpex_count} 筆")

    # 驗證
    total = con.execute("SELECT COUNT(*) FROM security_reference").fetchone()[0]
    con.close()
    print(f"\n✓ security_reference 合計: {total} 筆")


if __name__ == "__main__":
    fetch_and_upsert()
