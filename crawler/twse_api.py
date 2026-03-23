#!/usr/bin/env python3
"""TWSE / TPEx 每日行情爬蟲 + 漲停篩選。"""

import math
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


def _calc_limit_down(prev_close: float) -> float:
    """計算跌停價（10% 跌幅 + tick rule 無條件進位）。"""
    if prev_close <= 0:
        return 0
    raw = prev_close * 0.90
    if raw < 10:
        return math.ceil(raw * 100) / 100
    elif raw < 50:
        return math.ceil(raw * 20) / 20
    elif raw < 100:
        return math.ceil(raw * 10) / 10
    elif raw < 500:
        return math.ceil(raw * 2) / 2
    elif raw < 1000:
        return math.ceil(raw)
    else:
        return math.ceil(raw / 5) * 5


def _parse_roc_date(roc_str: str) -> datetime.date | None:
    """民國日期 → datetime.date。支援 '1150320' 和 '115/03/20' 兩種格式。"""
    try:
        s = roc_str.strip().replace("/", "")
        # 格式：YYYMMDD（民國年3碼 + 月2碼 + 日2碼 = 7碼）
        if len(s) == 7:
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
            return datetime.date(y, m, d)
        # fallback: 嘗試 YYY/MM/DD split
        parts = roc_str.strip().split("/")
        if len(parts) == 3:
            y = int(parts[0]) + 1911
            m = int(parts[1])
            d = int(parts[2])
            return datetime.date(y, m, d)
        return None
    except (ValueError, IndexError, AttributeError):
        return None


def _parse_mi_index_change(sign_html: str, change_abs: str) -> float:
    """解析 MI_INDEX 的漲跌符號（HTML tag）+ 漲跌價差（絕對值）。"""
    val = _safe_float(change_abs)
    if "color:green" in sign_html:  # 綠 = 跌
        return -val
    # red = 漲, 或無顏色 = 平盤
    return val


def _fetch_twse_mi_index(trade_date: datetime.date) -> tuple[list[dict], datetime.date | None]:
    """Fallback: 用 MI_INDEX 傳統 API 抓上市行情。"""
    date_str = trade_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("stat") != "OK":
        return [], None

    api_date = None
    raw_date = data.get("date", "")
    if raw_date:
        # 格式 '20260323'
        try:
            api_date = datetime.date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except (ValueError, IndexError):
            pass

    # table 8 = 每日收盤行情（全部不含權證牛熊證）
    table = None
    for t in data.get("tables", []):
        if "每日收盤行情" in t.get("title", ""):
            table = t
            break
    if not table or not table.get("data"):
        return [], api_date

    # fields: 證券代號(0), 證券名稱(1), 成交股數(2), 成交筆數(3), 成交金額(4),
    #         開盤價(5), 最高價(6), 最低價(7), 收盤價(8),
    #         漲跌(+/-)(9), 漲跌價差(10), ...
    results = []
    for row in table["data"]:
        code = str(row[0]).strip()
        if not code or len(code) > 6:
            continue
        close = _safe_float(row[8])
        open_p = _safe_float(row[5])
        high = _safe_float(row[6])
        low = _safe_float(row[7])
        change = _parse_mi_index_change(str(row[9]), str(row[10]))
        volume = _safe_int(row[2])
        trade_value = _safe_int(row[4])
        prev_close = close - change if close and change else 0
        limit_up = _calc_limit_up(prev_close) if prev_close > 0 else 0
        limit_down = round(prev_close * 0.90, 2) if prev_close > 0 else 0
        is_limit_up = close > 0 and limit_up > 0 and close >= limit_up - 0.05
        change_pct = round(change / prev_close * 100, 4) if prev_close > 0 else 0.0
        results.append({
            "stock_code": code,
            "stock_name": str(row[1]).strip(),
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
    return results, api_date


def fetch_twse(expected_date: datetime.date = None) -> tuple[list[dict], datetime.date | None]:
    """上市個股全部日成交資料。回傳 (rows, api_date)。
    若 OpenAPI 日期與 expected_date 不一致，自動 fallback 到 MI_INDEX。
    """
    resp = requests.get(TWSE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    # 從第一筆有效資料解析 API 日期
    api_date = None
    for r in rows:
        raw_date = r.get("Date", "").strip()
        if raw_date:
            api_date = _parse_roc_date(raw_date)
            break

    # 日期不一致 → fallback
    if expected_date and api_date and api_date != expected_date:
        print(f"  ⚠ TWSE OpenAPI 日期={api_date}，預期={expected_date}，改用 MI_INDEX fallback...")
        return _fetch_twse_mi_index(expected_date)

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
    return results, api_date


def fetch_tpex() -> tuple[list[dict], datetime.date | None]:
    """上櫃個股日收盤行情。"""
    resp = requests.get(TPEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if not resp.text.strip():
        print("  TPEx: 回傳空內容（非交易日?）")
        return [], None
    rows = resp.json()
    # 從第一筆有效資料解析 API 日期
    api_date = None
    for r in rows:
        raw_date = r.get("Date", "").strip()
        if raw_date:
            api_date = _parse_roc_date(raw_date)
            break
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
    return results, api_date


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

    twse_data, twse_date = fetch_twse(expected_date=trade_date)
    print(f"  TWSE: {len(twse_data)} 檔 (API date: {twse_date})")
    time.sleep(3)

    tpex_data, tpex_date = fetch_tpex()
    print(f"  TPEx: {len(tpex_data)} 檔 (API date: {tpex_date})")

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
