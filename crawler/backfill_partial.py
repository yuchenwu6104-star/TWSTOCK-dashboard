#!/usr/bin/env python3
"""補救最近 partial pipeline 日（TWSE 或 TPEx 缺失）。

掃 pipeline_status 找近 N 天 status='partial' 的紀錄，對最舊的一天嘗試重抓
缺失市場、重跑分類/LLM/分點，補成功後標記 ok。一次只處理最舊一天，避免
LLM 配額一次燒太多；若 API 仍未恢復就保留 partial 狀態，下次再試。
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb

from db.paths import DB_PATH
from crawler.twse_api import fetch_twse, fetch_tpex
from crawler.concept_classifier import (
    load_universe_map, classify_stocks, save_stock_meta,
)
from crawler.llm_summary import generate_theme_summaries
from crawler.broker_data import fetch_multiple_stocks, save_broker_data


LOOKBACK_DAYS = 5


def _find_partial_days() -> list[tuple[datetime.date, str]]:
    today = datetime.date.today()
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""
        SELECT date, warnings FROM pipeline_status
        WHERE status = 'partial' AND date >= ? AND date < ?
        ORDER BY date
    """, [today - datetime.timedelta(days=LOOKBACK_DAYS), today]).fetchall()
    con.close()
    return [(r[0], r[1] or "") for r in rows]


def _backfill_market(trade_date: datetime.date, market: str) -> bool:
    if market == "TWSE":
        rows, api_date = fetch_twse(expected_date=trade_date)
    elif market == "TPEx":
        rows, api_date = fetch_tpex(expected_date=trade_date)
    else:
        return False

    if api_date != trade_date or not rows:
        print(f"    ⚠ {market} API 仍未恢復（api_date={api_date}），下次再試")
        return False

    con = duckdb.connect(DB_PATH)
    con.execute("DELETE FROM daily_price WHERE date=? AND market=?", [trade_date, market])
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
    print(f"    ✓ 補入 {len(rows)} 筆 {market}")
    return True


def _update_status(trade_date: datetime.date, status: str, warnings: str):
    con = duckdb.connect(DB_PATH)
    con.execute("DELETE FROM pipeline_status WHERE date = ?", [trade_date])
    con.execute(
        "INSERT INTO pipeline_status VALUES (?, ?, ?, ?)",
        [trade_date, status, warnings, datetime.datetime.now()],
    )
    con.close()
    print(f"  ✓ pipeline_status[{trade_date}] → {status}")


def _rebuild_classification(trade_date: datetime.date):
    """重跑 daily_run 的 Step 3-6（不動 Step 7/8 避免破壞處置股當前狀態）。"""
    date_str = trade_date.strftime("%Y-%m-%d")

    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute("""
        SELECT stock_code, stock_name, close_price, volume, trade_value,
               change_pct, market
        FROM daily_price
        WHERE date = ? AND is_limit_up = true
        ORDER BY trade_value DESC
    """, [trade_date]).fetchall()
    con.close()

    limit_up = [{"stock_code": r[0], "stock_name": r[1],
                 "close_price": float(r[2]), "volume": r[3],
                 "trade_value": r[4], "change_pct": float(r[5]),
                 "market": r[6]} for r in rows]
    print(f"  漲停股: {len(limit_up)} 檔")

    if not limit_up:
        _update_status(trade_date, "ok", "")
        return

    print(f"  🏷 概念分類...")
    universe_map = load_universe_map()
    if universe_map:
        save_stock_meta(universe_map, DB_PATH)
    themes_raw = classify_stocks(limit_up, universe_map)

    print(f"  🤖 LLM 摘要...")
    theme_stocks_only = {name: info["stocks"] for name, info in themes_raw.items()}
    summaries = generate_theme_summaries(theme_stocks_only, date_str)

    code_name_map = {s["stock_name"]: s["stock_code"] for s in limit_up}
    all_reasons = {}
    for theme_name, sm in summaries.items():
        for key, reason in sm.get("stock_reasons", {}).items():
            if key.isdigit() and 4 <= len(key) <= 6:
                all_reasons[key] = reason
            elif key in code_name_map:
                all_reasons[code_name_map[key]] = reason

    con = duckdb.connect(DB_PATH)
    con.execute("DELETE FROM daily_theme WHERE date = ?", [trade_date])
    for theme_name, info in themes_raw.items():
        sm = summaries.get(theme_name, {})
        stock_codes = [s["stock_code"] for s in info["stocks"]]
        con.execute(
            "INSERT INTO daily_theme VALUES (?, ?, ?, ?, ?, ?)",
            [trade_date, theme_name, sm.get("summary", ""),
             sm.get("driver", ""), stock_codes, len(info["stocks"])],
        )
    con.execute("DELETE FROM stock_reason WHERE date = ?", [trade_date])
    for code, reason in all_reasons.items():
        con.execute(
            "INSERT INTO stock_reason VALUES (?, ?, ?)",
            [trade_date, code, reason],
        )
    con.close()
    print(f"  寫入 {len(themes_raw)} 個族群, {len(all_reasons)} 個個股原因")

    broker_codes = [s["stock_code"] for s in limit_up if len(s["stock_code"]) == 4]
    if broker_codes:
        print(f"  📊 券商分點 {len(broker_codes)} 檔...")
        broker_data = fetch_multiple_stocks(broker_codes)
        if broker_data:
            save_broker_data(broker_data, trade_date)

    _update_status(trade_date, "ok", "")


def main():
    partial = _find_partial_days()
    if not partial:
        print("✓ 近 5 天無 partial 紀錄，跳過")
        return

    print(f"🔧 偵測到 {len(partial)} 天 partial: {[str(d) for d, _ in partial]}")

    trade_date, warnings = partial[0]
    print(f"\n→ 嘗試補 {trade_date}（warnings: {warnings}）...")

    backfilled = False
    if "上市(TWSE)資料缺失" in warnings or "上市資料異常偏少" in warnings:
        if _backfill_market(trade_date, "TWSE"):
            backfilled = True
    if "上櫃(TPEx)資料缺失" in warnings or "上櫃資料異常偏少" in warnings:
        if _backfill_market(trade_date, "TPEx"):
            backfilled = True

    if not backfilled:
        print(f"  ⚠ 無 market 補成功，保留 partial 狀態")
        return

    print(f"\n→ 重跑分類/LLM/分點...")
    _rebuild_classification(trade_date)
    print(f"\n✅ {trade_date} 補救完成")


if __name__ == "__main__":
    main()
