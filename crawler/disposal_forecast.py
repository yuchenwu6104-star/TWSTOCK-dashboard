#!/usr/bin/env python3
"""處置預測模組：從 disposition_research.duckdb 算差幾次就處置。"""

import duckdb
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH

DISP_DB = "/Users/slking/taiwan_stock_dashboard/處置股研究/disposition_research.duckdb"

# 處置門檻定義
THRESHOLDS = [
    {"id": "3①", "window": 3, "need": 3, "rule_filter": "first_only",
     "desc": "連續3個營業日經第1款公布注意交易資訊即處置"},
    {"id": "5", "window": 5, "need": 5, "rule_filter": "any",
     "desc": "連續5個營業日經第1~8款公布注意交易資訊即處置"},
    {"id": "6/10", "window": 10, "need": 6, "rule_filter": "any",
     "desc": "最近10個營業日內有6日經第1~8款公布注意交易資訊即處置"},
    {"id": "12/30", "window": 30, "need": 12, "rule_filter": "any",
     "desc": "最近30個營業日內有12日經第1~8款公布注意交易資訊即處置"},
]


def _is_common_stock(symbol: str) -> bool:
    """只保留四位數普通股（排除權證、ETF、可轉債等）。"""
    return len(symbol) == 4 and symbol.isdigit()


def _get_trading_days(con, end_date: datetime.date, lookback: int = 35) -> list[datetime.date]:
    """從 attention_events 推算最近的營業日清單。"""
    rows = con.execute("""
        SELECT DISTINCT announcement_date
        FROM attention_events
        WHERE announcement_date <= ?
        ORDER BY announcement_date DESC
        LIMIT ?
    """, [end_date, lookback]).fetchall()
    return [r[0] for r in rows]


def _has_first_rule(rule_code: str) -> bool:
    """判斷 rule_code 是否包含第一款。"""
    if not rule_code:
        return False
    return "第一款" in rule_code


def compute_forecast(as_of_date: datetime.date = None) -> dict:
    """計算處置預測。

    回傳:
    {
        "as_of_date": "2026-03-18",
        "applicable_date": "2026-03-19",
        "in_disposal": [{"symbol", "name", "level", "start_date", "end_date", "rule_group", "interval"}],
        "almost": [{"symbol", "name", "count", "threshold", "rule_id", "rule_desc"}],  # 差1次
        "two_more": [{"symbol", "name", "count", "threshold", "rule_id", "rule_desc"}],  # 差2次
    }
    """
    if not os.path.exists(DISP_DB):
        print(f"⚠ disposition DB 不存在: {DISP_DB}")
        return {"in_disposal": [], "almost": [], "two_more": []}

    con = duckdb.connect(DISP_DB, read_only=True)

    if as_of_date is None:
        # 用 DB 裡最新的日期
        row = con.execute("SELECT MAX(announcement_date) FROM attention_events").fetchone()
        as_of_date = row[0] if row[0] else datetime.date.today()

    # --- 1. 處置中 ---
    in_disposal = []
    disp_rows = con.execute("""
        SELECT symbol, name, disposal_level, start_date, end_date,
               rule_group, matching_interval_seconds, exchange
        FROM disposal_events
        WHERE end_date >= ? AND start_date <= ?
        ORDER BY disposal_level DESC, symbol
    """, [as_of_date, as_of_date + datetime.timedelta(days=1)]).fetchall()

    in_disposal_symbols = set()
    for r in disp_rows:
        if not _is_common_stock(r[0]):
            continue
        interval_min = r[6] // 60 if r[6] else 5
        in_disposal.append({
            "symbol": r[0], "name": r[1], "level": r[2],
            "start_date": str(r[3]), "end_date": str(r[4]),
            "rule_group": r[5] or "",
            "interval": f"{interval_min}分鐘",
            "exchange": r[7] or "TWSE",
        })
        in_disposal_symbols.add(r[0])

    # --- 2. 取得營業日清單 ---
    trading_days = _get_trading_days(con, as_of_date, 35)
    if len(trading_days) < 3:
        con.close()
        return {"as_of_date": str(as_of_date), "in_disposal": in_disposal,
                "almost": [], "two_more": []}

    # --- 3. 取得近30日所有 attention events ---
    window_start = trading_days[min(len(trading_days) - 1, 29)]
    attn_rows = con.execute("""
        SELECT symbol, name, announcement_date, rule_code, exchange
        FROM attention_events
        WHERE announcement_date >= ? AND announcement_date <= ?
        ORDER BY symbol, announcement_date
    """, [window_start, as_of_date]).fetchall()

    # 整理成 {symbol: {name, exchange, dates: {date: [rule_codes]}}}
    stock_attn = {}
    for r in attn_rows:
        sym, name, dt, rule, exch = r[0], r[1], r[2], r[3] or "", r[4] or "TWSE"
        if not _is_common_stock(sym):
            continue
        if sym not in stock_attn:
            stock_attn[sym] = {"name": name, "exchange": exch, "dates": {}}
        if dt not in stock_attn[sym]["dates"]:
            stock_attn[sym]["dates"][dt] = []
        stock_attn[sym]["dates"][dt].append(rule)

    con.close()

    # --- 4. 每支股票 vs 每個門檻算次數 ---
    almost = []   # 差1次
    two_more = []  # 差2次

    for sym, info in stock_attn.items():
        if sym in in_disposal_symbols:
            continue  # 已在處置中，跳過

        for th in THRESHOLDS:
            window = th["window"]
            need = th["need"]
            rule_id = th["id"]

            # 取 window 內的營業日
            window_days = trading_days[:window] if len(trading_days) >= window else trading_days

            if th["id"] in ("3①", "5"):
                # 「連續」門檻：必須最近 N 個營業日連續命中
                count = 0
                for d in window_days:
                    if d in info["dates"]:
                        if th["rule_filter"] == "first_only":
                            if any(_has_first_rule(rc) for rc in info["dates"][d]):
                                count += 1
                            else:
                                break  # 連續中斷
                        else:
                            count += 1
                    else:
                        break  # 連續中斷
            else:
                # 「累計」門檻：window 內總計幾天
                count = 0
                for d in window_days:
                    if d in info["dates"]:
                        if th["rule_filter"] == "first_only":
                            if any(_has_first_rule(rc) for rc in info["dates"][d]):
                                count += 1
                        else:
                            count += 1

            gap = need - count
            if gap == 1:
                almost.append({
                    "symbol": sym, "name": info["name"],
                    "exchange": info.get("exchange", "TWSE"),
                    "count": count, "threshold": need,
                    "rule_id": rule_id, "rule_desc": th["desc"],
                })
            elif gap == 2:
                two_more.append({
                    "symbol": sym, "name": info["name"],
                    "exchange": info.get("exchange", "TWSE"),
                    "count": count, "threshold": need,
                    "rule_id": rule_id, "rule_desc": th["desc"],
                })

    # 去重：同一支股票可能在多個門檻都差1次，只保留最接近的
    almost_dedup = _dedup_closest(almost)
    two_more_dedup = _dedup_closest(two_more)

    # 算適用日期（下一個營業日）
    applicable = as_of_date + datetime.timedelta(days=1)
    while applicable.weekday() >= 5:
        applicable += datetime.timedelta(days=1)

    return {
        "as_of_date": str(as_of_date),
        "applicable_date": str(applicable),
        "in_disposal": in_disposal,
        "almost": sorted(almost_dedup, key=lambda x: x["threshold"] - x["count"]),
        "two_more": sorted(two_more_dedup, key=lambda x: x["threshold"] - x["count"]),
    }


def _dedup_closest(items: list[dict]) -> list[dict]:
    """同一支股票保留最接近處置門檻的那一條。"""
    best = {}
    for item in items:
        sym = item["symbol"]
        gap = item["threshold"] - item["count"]
        if sym not in best or gap < best[sym]["threshold"] - best[sym]["count"]:
            best[sym] = item
    return list(best.values())


def sync_to_local_db(forecast: dict, db_path: str = DB_PATH):
    """將處置中的股票同步到本地 disposal_stock 表。"""
    con = duckdb.connect(db_path)
    con.execute("DELETE FROM disposal_stock")
    for s in forecast["in_disposal"]:
        con.execute("""
            INSERT INTO disposal_stock VALUES (?, ?, ?, ?, ?, ?)
        """, [s["symbol"], s["name"], s["start_date"], s["end_date"],
              s["rule_group"], "active"])
    con.close()
    print(f"✓ disposal_stock 同步: {len(forecast['in_disposal'])} 筆處置中")


if __name__ == "__main__":
    result = compute_forecast()
    print(f"資料日期: {result['as_of_date']}")
    print(f"適用日期: {result['applicable_date']}")
    print(f"\n🔴 處置中: {len(result['in_disposal'])} 檔")
    for s in result["in_disposal"]:
        print(f"  {s['symbol']} {s['name']}  L{s['level']}  {s['start_date']}~{s['end_date']}  {s['rule_group']}  {s['interval']}")
    print(f"\n🟡 差1次就處置: {len(result['almost'])} 檔")
    for s in result["almost"]:
        print(f"  {s['symbol']} {s['name']}  {s['count']}/{s['threshold']}  {s['rule_id']}")
    print(f"\n🟠 差2次就處置: {len(result['two_more'])} 檔")
    for s in result["two_more"]:
        print(f"  {s['symbol']} {s['name']}  {s['count']}/{s['threshold']}  {s['rule_id']}")
