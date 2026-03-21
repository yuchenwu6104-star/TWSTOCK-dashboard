#!/usr/bin/env python3
"""每日主 pipeline：爬蟲 → 漲停篩選 → 概念分類 → LLM 摘要 → 券商分點 → 處置預測 → 生成網站。"""

import datetime
import duckdb
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.init_db import DB_PATH, init_db
from crawler.twse_api import fetch_twse, fetch_tpex, save_daily_prices
from crawler.concept_classifier import load_universe_map, classify_stocks, save_stock_meta
from crawler.llm_summary import generate_theme_summaries
from crawler.disposal_forecast import compute_forecast, sync_to_local_db
from crawler.broker_data import fetch_multiple_stocks, save_broker_data

DISP_DB = "/Users/slking/taiwan_stock_dashboard/處置股研究/disposition_research.duckdb"
BACKFILL_SCRIPT = "/Users/slking/taiwan_stock_dashboard/處置股研究/official_event_backfill.py"


def refresh_attention_events(trade_date: datetime.date):
    """刷新注意股/處置股公告。"""
    if not os.path.exists(BACKFILL_SCRIPT):
        print("  ⚠ backfill script not found, skip")
        return
    start = (trade_date - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    end = (trade_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        subprocess.run([
            sys.executable, BACKFILL_SCRIPT,
            "--db-path", DISP_DB,
            "--start-date", start, "--end-date", end,
            "--sources", "twse_attention", "twse_disposal", "tpex_attention", "tpex_disposal",
            "--sleep-seconds", "0.3",
        ], timeout=120, capture_output=True, text=True)
        print(f"  ✓ 注意股/處置股刷新完成")
    except Exception as e:
        print(f"  ⚠ 刷新失敗: {e}")


def _last_trading_day(d: datetime.date = None) -> datetime.date:
    """回傳最近的交易日（跳過週末）。"""
    if d is None:
        d = datetime.date.today()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= datetime.timedelta(days=1)
    return d


def run(trade_date: datetime.date = None, skip_broker: bool = False):
    if trade_date is None:
        trade_date = _last_trading_day()

    date_str = trade_date.strftime("%Y-%m-%d")
    print(f"{'='*50}")
    print(f"  台股漲停分析 Pipeline — {date_str}")
    print(f"{'='*50}")

    # Step 0: 初始化 DB
    init_db()

    # Step 1: 抓行情
    print(f"\n📥 Step 1: 抓取行情...")
    twse_data = fetch_twse()
    print(f"  TWSE: {len(twse_data)} 檔")

    time.sleep(3)
    tpex_data = fetch_tpex()
    print(f"  TPEx: {len(tpex_data)} 檔")

    all_data = twse_data + tpex_data
    save_daily_prices(all_data, trade_date)
    print(f"  寫入 DB: {len(all_data)} 筆")

    # Step 2: 篩選漲停
    limit_up = [r for r in all_data if r["is_limit_up"]]
    print(f"\n🔥 Step 2: 漲停篩選 → {len(limit_up)} 檔")
    if not limit_up:
        print("  今日無漲停股，結束。")
        return {"date": date_str, "themes": {}, "limit_up_count": 0}

    # Step 3: 概念分類
    print(f"\n🏷  Step 3: 概念分類...")
    universe_map = load_universe_map()
    if universe_map:
        save_stock_meta(universe_map, DB_PATH)
    themes_raw = classify_stocks(limit_up, universe_map)
    for name, info in themes_raw.items():
        codes = ", ".join(f"{s['stock_name']}({s['stock_code']})" for s in info["stocks"][:5])
        suffix = f" ...等{len(info['stocks'])}檔" if len(info["stocks"]) > 5 else ""
        print(f"  {info['icon']} {name}: {codes}{suffix}")

    # Step 4: LLM 摘要 + 個股原因
    print(f"\n🤖 Step 4: 生成族群摘要 + 個股原因...")
    theme_stocks_only = {name: info["stocks"] for name, info in themes_raw.items()}
    summaries = generate_theme_summaries(theme_stocks_only, date_str)

    # 彙整所有個股 reason
    all_reasons = {}
    for theme_name, sm in summaries.items():
        for code, reason in sm.get("stock_reasons", {}).items():
            all_reasons[code] = reason

    # Step 5: 寫入 daily_theme
    print(f"\n💾 Step 5: 寫入 daily_theme...")
    con = duckdb.connect(DB_PATH)
    con.execute("DELETE FROM daily_theme WHERE date = ?", [trade_date])
    for theme_name, info in themes_raw.items():
        sm = summaries.get(theme_name, {})
        stock_codes = [s["stock_code"] for s in info["stocks"]]
        con.execute("""
            INSERT INTO daily_theme VALUES (?, ?, ?, ?, ?, ?)
        """, [
            trade_date, theme_name,
            sm.get("summary", ""),
            sm.get("driver", ""),
            stock_codes,
            len(info["stocks"]),
        ])
    # 存個股 reason 到額外表
    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_reason (
            date DATE, stock_code VARCHAR, reason TEXT,
            PRIMARY KEY (date, stock_code)
        )
    """)
    con.execute("DELETE FROM stock_reason WHERE date = ?", [trade_date])
    for code, reason in all_reasons.items():
        con.execute("INSERT INTO stock_reason VALUES (?, ?, ?)", [trade_date, code, reason])
    con.close()
    print(f"  寫入 {len(themes_raw)} 個族群, {len(all_reasons)} 個個股原因")

    # Step 6: 券商分點（上市漲停股）
    if not skip_broker:
        twse_limit_up_codes = [s["stock_code"] for s in limit_up
                               if s["market"] == "TWSE" and len(s["stock_code"]) == 4]
        if twse_limit_up_codes:
            print(f"\n📊 Step 6: 券商分點（{len(twse_limit_up_codes)} 檔上市漲停股）...")
            broker_data = fetch_multiple_stocks(twse_limit_up_codes)
            if broker_data:
                save_broker_data(broker_data, trade_date)
        else:
            print(f"\n📊 Step 6: 無上市漲停股，跳過券商分點")
    else:
        print(f"\n📊 Step 6: 跳過券商分點（skip_broker=True）")

    # Step 7: 刷新注意股/處置股
    print(f"\n🚨 Step 7: 刷新注意股/處置股...")
    refresh_attention_events(trade_date)

    # Step 8: 處置預測
    print(f"\n🔮 Step 8: 處置預測...")
    forecast = compute_forecast(trade_date)
    sync_to_local_db(forecast, DB_PATH)
    print(f"  處置中: {len(forecast['in_disposal'])} 檔")
    print(f"  差1次: {len(forecast['almost'])} 檔")
    print(f"  差2次: {len(forecast['two_more'])} 檔")

    # 結果
    result = {
        "date": date_str,
        "limit_up_count": len(limit_up),
        "theme_count": len(themes_raw),
        "themes": {
            name: {
                "stocks": [{"code": s["stock_code"], "name": s["stock_name"],
                            "price": s["close_price"], "volume": s["volume"]}
                           for s in info["stocks"]],
                "icon": info["icon"],
                "summary": summaries.get(name, {}).get("summary", ""),
                "driver": summaries.get(name, {}).get("driver", ""),
            }
            for name, info in themes_raw.items()
        },
        "disposal_forecast": forecast,
        "stock_reasons": all_reasons,
    }

    print(f"\n✅ 完成！{len(limit_up)} 檔漲停，{len(themes_raw)} 個族群")
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--skip-broker", action="store_true", help="跳過券商分點")
    args = parser.parse_args()
    if args.date:
        d = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        d = None
    run(d, skip_broker=args.skip_broker)
