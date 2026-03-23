#!/usr/bin/env python3
"""從 market_daily.duckdb 匯入歷史 OHLCV 到本地 DB，含漲停判斷。"""

import duckdb
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.paths import DB_PATH, MARKET_DB
from db.init_db import init_db
from crawler.twse_api import _calc_limit_up

# MARKET_DB imported from db.paths


def import_date(trade_date: datetime.date):
    """從 market_daily.duckdb 匯入單日資料。"""
    if not os.path.exists(MARKET_DB):
        print(f"⚠ market_daily.duckdb 不存在")
        return 0

    init_db()
    mkt_con = duckdb.connect(MARKET_DB, read_only=True)

    # 取當日 + 前一日（算漲停用）
    prev_date = mkt_con.execute("""
        SELECT MAX(trade_date) FROM daily_ohlcv WHERE trade_date < ?
    """, [trade_date]).fetchone()[0]

    if not prev_date:
        mkt_con.close()
        return 0

    # 前一日收盤
    prev_rows = mkt_con.execute("""
        SELECT symbol, close FROM daily_ohlcv WHERE trade_date = ?
    """, [prev_date]).fetchall()
    prev_close_map = {r[0]: float(r[1]) if r[1] else 0 for r in prev_rows}

    # 當日 OHLCV
    rows = mkt_con.execute("""
        SELECT symbol, name, exchange, open, high, low, close, volume, trade_value
        FROM daily_ohlcv WHERE trade_date = ?
    """, [trade_date]).fetchall()
    mkt_con.close()

    if not rows:
        return 0

    local_con = duckdb.connect(DB_PATH)
    local_con.execute("DELETE FROM daily_price WHERE date = ?", [trade_date])

    count = 0
    for r in rows:
        sym, name, exchange, op, hi, lo, cl, vol, tv = r
        cl = float(cl) if cl else 0
        op = float(op) if op else 0
        hi = float(hi) if hi else 0
        lo = float(lo) if lo else 0
        vol = int(vol) if vol else 0
        tv = int(tv) if tv else 0

        if cl <= 0 or not sym:
            continue

        prev_cl = prev_close_map.get(sym, 0)
        if prev_cl <= 0:
            continue

        change = round(cl - prev_cl, 4)
        change_pct = round(change / prev_cl * 100, 4)
        limit_up = _calc_limit_up(prev_cl)
        limit_down = round(prev_cl * 0.90, 2)
        is_limit_up = cl >= limit_up - 0.05 if limit_up > 0 else False

        market = exchange if exchange else ("TWSE" if len(sym) == 4 else "TPEx")

        local_con.execute("""
            INSERT OR REPLACE INTO daily_price VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [trade_date, sym, name or "", op, hi, lo, cl,
              limit_up, limit_down, is_limit_up, vol, tv, change, change_pct, market])
        count += 1

    local_con.close()
    return count


def import_range(start_date: datetime.date, end_date: datetime.date):
    """匯入日期範圍。"""
    if not os.path.exists(MARKET_DB):
        return

    mkt_con = duckdb.connect(MARKET_DB, read_only=True)
    dates = mkt_con.execute("""
        SELECT DISTINCT trade_date FROM daily_ohlcv
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """, [start_date, end_date]).fetchall()
    mkt_con.close()

    total = 0
    for row in dates:
        d = row[0]
        n = import_date(d)
        if n > 0:
            # 統計漲停數
            con = duckdb.connect(DB_PATH, read_only=True)
            lu = con.execute("SELECT COUNT(*) FROM daily_price WHERE date=? AND is_limit_up=true", [d]).fetchone()[0]
            con.close()
            print(f"  {d}: {n} 筆, 漲停 {lu} 檔")
            total += n

    print(f"✓ 合計匯入 {total} 筆, {len(dates)} 天")
    return total


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, required=True, help="起始日 YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="結束日 YYYY-MM-DD")
    args = parser.parse_args()
    s = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
    e = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    import_range(s, e)
