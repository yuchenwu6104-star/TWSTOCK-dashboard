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

from db.paths import DB_PATH, DISP_DB, BACKFILL_SCRIPT
from db.init_db import init_db
from crawler.twse_api import fetch_twse, fetch_tpex, save_daily_prices
from crawler.concept_classifier import load_universe_map, classify_stocks, save_stock_meta
from crawler.llm_summary import generate_theme_summaries
from crawler.disposal_forecast import compute_forecast, sync_to_local_db
from crawler.broker_data import fetch_multiple_stocks, save_broker_data

# DISP_DB imported from db.paths
# BACKFILL_SCRIPT imported from db.paths


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
    """回傳最近的交易日（跳過週末 + 國定假日）。"""
    if d is None:
        d = datetime.date.today()
    while d.weekday() >= 5 or d in _TW_HOLIDAYS:
        d -= datetime.timedelta(days=1)
    return d


# 台股國定假日（2026 年）
# 來源：TWSE 休市日曆 https://www.twse.com.tw/zh/trading/holiday.html
_TW_HOLIDAYS = {
    # 2026
    datetime.date(2026, 1, 1),   # 元旦
    datetime.date(2026, 1, 2),   # 彈性放假
    datetime.date(2026, 1, 26),  # 除夕前
    datetime.date(2026, 1, 27),  # 除夕
    datetime.date(2026, 1, 28),  # 初一
    datetime.date(2026, 1, 29),  # 初二
    datetime.date(2026, 1, 30),  # 初三
    datetime.date(2026, 2, 2),   # 彈性放假
    datetime.date(2026, 2, 27),  # 和平紀念日（調整）
    datetime.date(2026, 2, 28),  # 和平紀念日
    datetime.date(2026, 4, 3),   # 兒童節（調整）
    datetime.date(2026, 4, 4),   # 清明節
    datetime.date(2026, 4, 5),   # 兒童節
    datetime.date(2026, 4, 6),   # 彈性放假
    datetime.date(2026, 5, 25),  # 端午節（調整）
    datetime.date(2026, 6, 19),  # 彈性放假
    datetime.date(2026, 9, 28),  # 中秋節（調整）
    datetime.date(2026, 10, 9),  # 彈性放假
    datetime.date(2026, 10, 10), # 國慶日
    # 每年初需更新
}


def _save_pipeline_status(trade_date: datetime.date, warnings: list[str]):
    """寫入 pipeline 執行狀態，供 build_site 顯示警告橫幅。"""
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_status (
            date DATE PRIMARY KEY,
            status VARCHAR,
            warnings VARCHAR,
            updated_at TIMESTAMP
        )
    """)
    status = "partial" if warnings else "ok"
    warnings_str = " ／ ".join(warnings) if warnings else ""
    con.execute("DELETE FROM pipeline_status WHERE date = ?", [trade_date])
    con.execute("INSERT INTO pipeline_status VALUES (?, ?, ?, ?)",
                [trade_date, status, warnings_str, datetime.datetime.now()])
    con.close()


def run(trade_date: datetime.date = None, skip_broker: bool = False):
    if trade_date is None:
        trade_date = _last_trading_day()

    date_str = trade_date.strftime("%Y-%m-%d")
    print(f"{'='*50}")
    print(f"  台股漲停分析 Pipeline — {date_str}")
    print(f"{'='*50}")

    # Step 0: 初始化 DB
    init_db()

    # Step 1: 抓行情（含 retry）
    print(f"\n📥 Step 1: 抓取行情...")
    twse_data, twse_api_date = [], None
    for attempt in range(3):
        try:
            twse_data, twse_api_date = fetch_twse(expected_date=trade_date)
            print(f"  TWSE: {len(twse_data)} 檔 (API date: {twse_api_date})")
            break
        except Exception as e:
            print(f"  ⚠ TWSE attempt {attempt+1} failed: {e}")
            time.sleep(5)

    time.sleep(3)
    tpex_data, tpex_api_date = [], None
    for attempt in range(3):
        try:
            tpex_data, tpex_api_date = fetch_tpex(expected_date=trade_date)
            print(f"  TPEx: {len(tpex_data)} 檔 (API date: {tpex_api_date})")
            break
        except Exception as e:
            print(f"  ⚠ TPEx attempt {attempt+1} failed: {e}")
            time.sleep(5)

    # 日期驗證：API 回傳日期必須與 trade_date 一致，不一致則丟棄該市場資料
    if twse_data and twse_api_date and twse_api_date != trade_date:
        print(f"  ⚠ TWSE 日期不一致！API={twse_api_date}, 預期={trade_date}，丟棄 {len(twse_data)} 筆上市資料")
        twse_data = []
    if tpex_data and tpex_api_date and tpex_api_date != trade_date:
        print(f"  ⚠ TPEx 日期不一致！API={tpex_api_date}, 預期={trade_date}，丟棄 {len(tpex_data)} 筆上櫃資料")
        tpex_data = []

    if not twse_data and not tpex_data:
        print("  ❌ 兩邊都沒有有效資料（API 異常或日期不一致），結束。")
        return {"date": date_str, "themes": {}, "limit_up_count": 0, "warnings": ["TWSE 和 TPEx 都無資料"]}

    # 部分資料檢查
    warnings = []
    if not twse_data:
        warnings.append("上市(TWSE)資料缺失")
        print("  ⚠⚠ 警告：無上市資料，僅有上櫃資料！")
    elif len(twse_data) < 1000:
        warnings.append(f"上市資料異常偏少({len(twse_data)}筆)")
        print(f"  ⚠⚠ 警告：上市資料僅 {len(twse_data)} 筆，可能不完整！")
    if not tpex_data:
        warnings.append("上櫃(TPEx)資料缺失")
        print("  ⚠⚠ 警告：無上櫃資料，僅有上市資料！")
    elif len(tpex_data) < 500:
        warnings.append(f"上櫃資料異常偏少({len(tpex_data)}筆)")
        print(f"  ⚠⚠ 警告：上櫃資料僅 {len(tpex_data)} 筆，可能不完整！")

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

    # 彙整所有個股 reason（修正 key：LLM 有時回傳公司名而非代號）
    code_name_map = {s["stock_name"]: s["stock_code"] for s in limit_up}
    all_reasons = {}
    for theme_name, sm in summaries.items():
        for key, reason in sm.get("stock_reasons", {}).items():
            # key 應該是股票代號（純數字 4-6 碼），否則用公司名反查
            if key.isdigit() and 4 <= len(key) <= 6:
                all_reasons[key] = reason
            elif key in code_name_map:
                all_reasons[code_name_map[key]] = reason
                print(f"  ⚠ stock_reason key 修正: {key} → {code_name_map[key]}")
            else:
                print(f"  ⚠ stock_reason key 無法識別，跳過: {key}")

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

    # Step 6: 券商分點（上市+上櫃漲停股，排除 ETF/權證）
    if not skip_broker:
        broker_codes = [s["stock_code"] for s in limit_up
                        if len(s["stock_code"]) == 4]
        if broker_codes:
            print(f"\n📊 Step 6: 券商分點（{len(broker_codes)} 檔漲停股）...")
            broker_data = fetch_multiple_stocks(broker_codes)
            if broker_data:
                save_broker_data(broker_data, trade_date)
        else:
            print(f"\n📊 Step 6: 無漲停股，跳過券商分點")
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
        "warnings": warnings,
    }

    # 寫入 pipeline 狀態供 build_site 讀取
    _save_pipeline_status(trade_date, warnings)

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
