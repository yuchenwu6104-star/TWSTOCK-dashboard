#!/usr/bin/env python3
"""TWSE / TPEx 每日行情爬蟲 + 漲停篩選。"""

import requests
import duckdb
import datetime
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH, init_db

# ---------- API ----------

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _safe_float(val, default=0.0):
    try:
        v = str(val).replace(",", "").strip()
        return float(v) if v else default
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0):
    try:
        v = str(val).replace(",", "").strip()
        return int(float(v)) if v else default
    except (ValueError, TypeError):
        return default


def _calc_limit_up(prev_close: float) -> float:
    """計算漲停價（10% 漲幅 + tick rule 無條件捨去）。

    台股漲停價 = 前收 × 1.10，無條件捨去到該價位級距的 tick。
    價位級距（TWSE/TPEx 通用）：
      < 10    → tick 0.01
      < 50    → tick 0.05
      < 100   → tick 0.10
      < 500   → tick 0.50
      < 1000  → tick 1.00
      >= 1000 → tick 5.00
    """
    import math
    if prev_close <= 0:
        return 0
    raw = prev_close * 1.10
    if raw < 10:
        return math.floor(raw * 100) / 100
    elif raw < 50:
        return math.floor(raw * 20) / 20
    elif raw < 100:
        return math.floor(raw * 10) / 10
    elif raw < 500:
        return math.floor(raw * 2) / 2
    elif raw < 1000:
        return math.floor(raw)
    else:
        return math.floor(raw / 5) * 5


def fetch_twse() -> list[dict]:
    """上市個股全部日成交資料。"""
    resp = requests.get(TWSE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    results = []
    for r in rows:
        code = r.get("Code", "").strip()
        if not code or len(code) > 6:
            continue
        close = _safe_float(r.get("ClosingPrice"))
        open_p = _safe_float(r.get("OpeningPrice"))
        high = _safe_float(r.get("HighestPrice"))
        low = _safe_float(r.get("LowestPrice"))
        change = _safe_float(r.get("Change"))
        volume = _safe_int(r.get("TradeVolume"))
        trade_value = _safe_int(r.get("TradeValue"))
        prev_close = close - change if close and change else 0
        limit_up = _calc_limit_up(prev_close) if prev_close > 0 else 0
        limit_down = round(prev_close * 0.90, 2) if prev_close > 0 else 0
        # 漲停判定：收盤價 >= 漲停價（容差 0.05）
        is_limit_up = close > 0 and limit_up > 0 and close >= limit_up - 0.05
        change_pct = round(change / prev_close * 100, 4) if prev_close > 0 else 0.0
        results.append({
            "stock_code": code,
            "stock_name": r.get("Name", "").strip(),
            "open_price": open_p,
            "high_price": high,
            "low_price": low,
            "close_price": close,
            "limit_up_price": limit_up,
            "limit_down_price": limit_down,
            "is_limit_up": is_limit_up,
            "volume": volume,
            "trade_value": trade_value,
            "change_price": change,
            "change_pct": change_pct,
            "market": "TWSE",
        })
    return results


def fetch_tpex() -> list[dict]:
    """上櫃個股日收盤行情。"""
    resp = requests.get(TPEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if not resp.text.strip():
        print("  TPEx: 回傳空內容（非交易日?）")
        return []
    rows = resp.json()
    results = []
    for r in rows:
        code = r.get("SecuritiesCompanyCode", "").strip()
        if not code or len(code) > 6:
            continue
        close = _safe_float(r.get("Close"))
        open_p = _safe_float(r.get("Open"))
        high = _safe_float(r.get("High"))
        low = _safe_float(r.get("Low"))
        change = _safe_float(r.get("Change"))
        volume = _safe_int(r.get("TradingShares"))
        trade_value = _safe_int(r.get("TransactionAmount"))
        prev_close = close - change if close and change else 0
        limit_up = _calc_limit_up(prev_close) if prev_close > 0 else 0
        limit_down = round(prev_close * 0.90, 2) if prev_close > 0 else 0
        is_limit_up = close > 0 and limit_up > 0 and close >= limit_up - 0.05
        change_pct = round(change / prev_close * 100, 4) if prev_close > 0 else 0.0
        results.append({
            "stock_code": code,
            "stock_name": r.get("CompanyName", "").strip(),
            "open_price": open_p,
            "high_price": high,
            "low_price": low,
            "close_price": close,
            "limit_up_price": limit_up,
            "limit_down_price": limit_down,
            "is_limit_up": is_limit_up,
            "volume": volume,
            "trade_value": trade_value,
            "change_price": change,
            "change_pct": change_pct,
            "market": "TPEx",
        })
    return results


# ---------- 寫入 DB ----------

def save_daily_prices(rows: list[dict], trade_date: datetime.date, db_path: str = DB_PATH):
    """寫入 daily_price 表。"""
    if not rows:
        return
    con = duckdb.connect(db_path)
    # 先刪舊資料（冪等）
    con.execute("DELETE FROM daily_price WHERE date = ?", [trade_date])
    for r in rows:
        con.execute("""
            INSERT INTO daily_price VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            trade_date, r["stock_code"], r["stock_name"],
            r["open_price"], r["high_price"], r["low_price"], r["close_price"],
            r["limit_up_price"], r["limit_down_price"], r["is_limit_up"],
            r["volume"], r["trade_value"], r["change_price"], r["change_pct"],
            r["market"],
        ])
    con.close()


def get_limit_up_stocks(trade_date: datetime.date, db_path: str = DB_PATH) -> list[dict]:
    """從 DB 撈當日漲停股。"""
    con = duckdb.connect(db_path, read_only=True)
    df = con.execute("""
        SELECT stock_code, stock_name, close_price, volume, trade_value,
               change_pct, market
        FROM daily_price
        WHERE date = ? AND is_limit_up = true
        ORDER BY trade_value DESC
    """, [trade_date]).fetchdf()
    con.close()
    return df.to_dict("records")


# ---------- CLI ----------

def run(trade_date: datetime.date = None):
    if trade_date is None:
        trade_date = datetime.date.today()

    init_db()
    print(f"📥 抓取 {trade_date} 行情...")

    twse_data = fetch_twse()
    print(f"  TWSE: {len(twse_data)} 檔")
    time.sleep(3)

    tpex_data = fetch_tpex()
    print(f"  TPEx: {len(tpex_data)} 檔")

    all_data = twse_data + tpex_data
    save_daily_prices(all_data, trade_date)
    print(f"  寫入 DB: {len(all_data)} 筆")

    limit_up = [r for r in all_data if r["is_limit_up"]]
    print(f"🔥 當日漲停: {len(limit_up)} 檔")
    for s in sorted(limit_up, key=lambda x: x["trade_value"], reverse=True)[:20]:
        print(f"  {s['stock_code']} {s['stock_name']}  ${s['close_price']}  量{s['volume']:,}")

    return all_data


if __name__ == "__main__":
    run()
