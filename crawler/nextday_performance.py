#!/usr/bin/env python3
"""隔日報酬計算模組：漲停股 T+1 開盤/均價/收盤報酬 + 標籤 + 族群勝率。"""

import duckdb
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH

MARKET_DB = "/Users/slking/taiwan_stock_dashboard/收盤觀察/market_daily.duckdb"


def _get_next_trading_day(con, after_date: datetime.date) -> datetime.date | None:
    """從 market_daily.duckdb 找 after_date 之後最近的交易日。"""
    r = con.execute("""
        SELECT MIN(trade_date) FROM daily_ohlcv
        WHERE trade_date > ?
    """, [after_date]).fetchone()
    return r[0] if r and r[0] else None


def _get_trading_days_before(con, before_date: datetime.date, n: int) -> list[datetime.date]:
    """找 before_date 之前（含）的 n 個交易日。"""
    rows = con.execute("""
        SELECT DISTINCT trade_date FROM daily_ohlcv
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
    """, [before_date, n]).fetchall()
    return [r[0] for r in rows]


def compute_nextday(limit_up_date: datetime.date) -> dict:
    """計算某日漲停股的隔日報酬。

    回傳:
    {
        "limit_up_date": "2026-03-19",
        "next_date": "2026-03-20",
        "stats": {
            "open_return": 5.43, "open_positive_rate": 90.6,
            "avg_return": 3.43, "avg_positive_rate": 73.4,
            "close_return": 1.45, "close_positive_rate": 56.3,
            "continuation_count": 15, "total_limit_up": 64,
        },
        "stocks": [
            {"code", "name", "theme", "exchange",
             "limit_up_price", "volume_ratio",
             "next_open", "next_high", "next_low", "next_close",
             "open_return", "avg_return", "close_return",
             "next_is_limit_up", "label", "label_color"},
        ],
        "theme_stats": [
            {"theme", "icon", "count", "win_rate", "win_count",
             "avg_open_return", "avg_close_return"},
        ],
    }
    """
    if not os.path.exists(MARKET_DB):
        print(f"⚠ market_daily.duckdb 不存在")
        return _empty_result(limit_up_date)

    # 讀本地 DB 漲停股
    local_con = duckdb.connect(DB_PATH, read_only=True)
    limit_up_rows = local_con.execute("""
        SELECT stock_code, stock_name, close_price, change_pct, volume, market
        FROM daily_price
        WHERE date = ? AND is_limit_up = true
    """, [limit_up_date]).fetchall()

    # 讀族群分類
    theme_rows = local_con.execute("""
        SELECT theme_name, stock_list FROM daily_theme WHERE date = ?
    """, [limit_up_date]).fetchall()
    code_to_theme = {}
    for name, codes in theme_rows:
        if codes:
            for c in codes:
                code_to_theme[c] = name

    local_con.close()

    if not limit_up_rows:
        return _empty_result(limit_up_date)

    # 從 market_daily 取隔日 OHLCV
    mkt_con = duckdb.connect(MARKET_DB, read_only=True)
    next_date = _get_next_trading_day(mkt_con, limit_up_date)

    if not next_date:
        mkt_con.close()
        print(f"⚠ {limit_up_date} 之後找不到交易日（隔日資料尚未入庫）")
        return _empty_result(limit_up_date)

    # 批次取隔日 OHLCV
    codes = [r[0] for r in limit_up_rows]
    placeholders = ",".join(["?"] * len(codes))
    next_ohlcv = mkt_con.execute(f"""
        SELECT symbol, open, high, low, close, volume
        FROM daily_ohlcv
        WHERE trade_date = ? AND symbol IN ({placeholders})
    """, [next_date] + codes).fetchall()
    next_map = {r[0]: {"open": float(r[1]) if r[1] else 0, "high": float(r[2]) if r[2] else 0,
                        "low": float(r[3]) if r[3] else 0, "close": float(r[4]) if r[4] else 0,
                        "volume": int(r[5]) if r[5] else 0}
                for r in next_ohlcv}

    # 取隔日漲停股（判斷續漲停）
    next_limit_up_rows = mkt_con.execute(f"""
        SELECT symbol, close, volume FROM daily_ohlcv
        WHERE trade_date = ? AND symbol IN ({placeholders})
    """, [next_date] + codes).fetchall()

    # 取前一日成交量（算量比）
    prev_vol = {}
    prev_date_rows = mkt_con.execute(f"""
        SELECT symbol, volume FROM daily_ohlcv
        WHERE trade_date = ? AND symbol IN ({placeholders})
    """, [limit_up_date] + codes).fetchall()
    for r in prev_date_rows:
        prev_vol[r[0]] = int(r[1]) if r[1] else 0

    mkt_con.close()

    # 計算每支股票的隔日報酬
    stocks = []
    open_returns = []
    avg_returns = []
    close_returns = []
    continuation_count = 0

    for r in limit_up_rows:
        code, name, close_price, change_pct, volume, market = r
        close_p = float(close_price) if close_price else 0
        nxt = next_map.get(code, {})
        n_open = nxt.get("open", 0)
        n_high = nxt.get("high", 0)
        n_low = nxt.get("low", 0)
        n_close = nxt.get("close", 0)

        if close_p <= 0 or n_open <= 0:
            continue

        # 報酬率
        o_ret = round((n_open / close_p - 1) * 100, 2)
        avg_price = (n_high + n_low) / 2 if n_high and n_low else n_close
        a_ret = round((avg_price / close_p - 1) * 100, 2)
        c_ret = round((n_close / close_p - 1) * 100, 2)

        # 續漲停判斷（隔日收盤 >= 隔日漲停價 ≈ 收盤*1.10）
        next_limit_up = n_close >= close_p * 1.095  # 寬鬆判定
        if next_limit_up:
            continuation_count += 1

        # 量比
        prev_v = prev_vol.get(code, 0)
        vol_ratio = round(nxt.get("volume", 0) / prev_v, 2) if prev_v > 0 else 0

        # 標籤
        label, label_color = _classify_label(o_ret, c_ret, next_limit_up)

        stocks.append({
            "code": code, "name": name,
            "theme": code_to_theme.get(code, ""),
            "exchange": market or "TWSE",
            "limit_up_price": close_p,
            "volume_ratio": vol_ratio,
            "next_open": n_open, "next_high": n_high,
            "next_low": n_low, "next_close": n_close,
            "open_return": o_ret, "avg_return": a_ret, "close_return": c_ret,
            "next_is_limit_up": next_limit_up,
            "label": label, "label_color": label_color,
        })
        open_returns.append(o_ret)
        avg_returns.append(a_ret)
        close_returns.append(c_ret)

    # 彙總統計
    total = len(stocks)
    stats = {
        "open_return": round(sum(open_returns) / total, 2) if total else 0,
        "open_positive_rate": round(sum(1 for r in open_returns if r > 0) / total * 100, 1) if total else 0,
        "avg_return": round(sum(avg_returns) / total, 2) if total else 0,
        "avg_positive_rate": round(sum(1 for r in avg_returns if r > 0) / total * 100, 1) if total else 0,
        "close_return": round(sum(close_returns) / total, 2) if total else 0,
        "close_positive_rate": round(sum(1 for r in close_returns if r > 0) / total * 100, 1) if total else 0,
        "continuation_count": continuation_count,
        "total_limit_up": total,
    }

    # 族群勝率
    theme_map = {}
    for s in stocks:
        th = s["theme"] or "其他"
        if th not in theme_map:
            theme_map[th] = {"stocks": [], "open_rets": [], "close_rets": []}
        theme_map[th]["stocks"].append(s)
        theme_map[th]["open_rets"].append(s["open_return"])
        theme_map[th]["close_rets"].append(s["close_return"])

    theme_stats = []
    for th, data in sorted(theme_map.items(), key=lambda x: len(x[1]["stocks"]), reverse=True):
        cnt = len(data["stocks"])
        win = sum(1 for r in data["close_rets"] if r > 0)
        theme_stats.append({
            "theme": th,
            "count": cnt,
            "win_count": win,
            "win_rate": round(win / cnt * 100, 1) if cnt else 0,
            "avg_open_return": round(sum(data["open_rets"]) / cnt, 2) if cnt else 0,
            "avg_close_return": round(sum(data["close_rets"]) / cnt, 2) if cnt else 0,
        })

    # 排序：報酬率高的在前
    stocks.sort(key=lambda x: x["close_return"], reverse=True)

    return {
        "limit_up_date": str(limit_up_date),
        "next_date": str(next_date),
        "stats": stats,
        "stocks": stocks,
        "theme_stats": theme_stats,
    }


def _classify_label(open_ret: float, close_ret: float, is_limit_up: bool) -> tuple[str, str]:
    """分類隔日表現標籤。"""
    if is_limit_up:
        return "續漲停", "red"
    if close_ret >= 5:
        return "強續漲", "red"
    if close_ret > 0 and open_ret > 0:
        return "開高續漲", "orange"
    if close_ret > 0 and open_ret <= 0:
        return "開低拉回", "blue"
    if close_ret <= 0 and open_ret > 3:
        return "開高走低", "yellow"
    if close_ret <= -3:
        return "直接跌", "green"
    if close_ret <= 0:
        return "小跌", "gray"
    return "", "gray"


def compute_history(as_of_date: datetime.date, lookback_days: int = 10) -> list[dict]:
    """計算最近 N 天的每日隔日報酬統計（給折線圖用）。

    回傳 [{"date", "open_return", "avg_return", "close_return",
           "open_positive_rate", "close_positive_rate", "total"}]
    """
    if not os.path.exists(MARKET_DB):
        return []

    mkt_con = duckdb.connect(MARKET_DB, read_only=True)
    trading_days = _get_trading_days_before(mkt_con, as_of_date, lookback_days + 5)
    mkt_con.close()

    local_con = duckdb.connect(DB_PATH, read_only=True)
    # 找有 daily_theme 的日期
    available = local_con.execute("""
        SELECT DISTINCT date FROM daily_theme ORDER BY date DESC
    """).fetchall()
    local_con.close()
    avail_dates = [r[0] for r in available]

    history = []
    for d in avail_dates[:lookback_days]:
        result = compute_nextday(d)
        if result["stats"]["total_limit_up"] > 0:
            history.append({
                "date": str(d),
                "open_return": result["stats"]["open_return"],
                "avg_return": result["stats"]["avg_return"],
                "close_return": result["stats"]["close_return"],
                "open_positive_rate": result["stats"]["open_positive_rate"],
                "close_positive_rate": result["stats"]["close_positive_rate"],
                "total": result["stats"]["total_limit_up"],
            })

    history.reverse()  # 舊→新
    return history


def _empty_result(d):
    return {
        "limit_up_date": str(d), "next_date": "",
        "stats": {"open_return": 0, "open_positive_rate": 0, "avg_return": 0,
                  "avg_positive_rate": 0, "close_return": 0, "close_positive_rate": 0,
                  "continuation_count": 0, "total_limit_up": 0},
        "stocks": [], "theme_stats": [],
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="漲停日 YYYY-MM-DD")
    args = parser.parse_args()

    d = datetime.datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.date(2026, 3, 19)
    result = compute_nextday(d)

    print(f"漲停日: {result['limit_up_date']} → 隔日: {result['next_date']}")
    st = result["stats"]
    print(f"漲停股: {st['total_limit_up']} 檔, 續漲停: {st['continuation_count']}")
    print(f"開盤: {st['open_return']:+.2f}% (勝率{st['open_positive_rate']}%)")
    print(f"均價: {st['avg_return']:+.2f}% (勝率{st['avg_positive_rate']}%)")
    print(f"收盤: {st['close_return']:+.2f}% (勝率{st['close_positive_rate']}%)")

    print(f"\n族群勝率:")
    for ts in result["theme_stats"]:
        print(f"  {ts['theme']:15s} {ts['win_count']}/{ts['count']} ({ts['win_rate']}%) 開{ts['avg_open_return']:+.2f}% 收{ts['avg_close_return']:+.2f}%")

    print(f"\n個股明細 (前10):")
    for s in result["stocks"][:10]:
        print(f"  {s['code']} {s['name']:8s} [{s['label']:4s}] 開{s['open_return']:+.2f}% 收{s['close_return']:+.2f}%  {s['theme']}")
