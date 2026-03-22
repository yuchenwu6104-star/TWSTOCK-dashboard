#!/usr/bin/env python3
"""統計彙總模組：多天的趨勢、族群熱力圖、個股績效。"""

import datetime
import duckdb
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH
from crawler.nextday_performance import compute_nextday


def compute_stats(latest_date: datetime.date, lookback_days: int = 10) -> dict:
    """彙總多天統計。

    回傳:
    {
        "dates": ["2026-03-17", ...],
        "trend": [{"date", "total", "continuation", "open_return", "avg_return", "close_return"}, ...],
        "heatmap": {"theme_name": [{"date", "close_return", "count"}, ...], ...},
        "theme_table": [{"theme", "days", "count", "open_return", "avg_return", "close_return"}, ...],
        "stock_table": [{"code", "name", "theme", "days", "open_return", "avg_return", "close_return"}, ...],
    }
    """
    con = duckdb.connect(DB_PATH, read_only=True)
    avail = con.execute("""
        SELECT DISTINCT date FROM daily_theme ORDER BY date DESC
    """).fetchall()
    con.close()
    avail_dates = [r[0] for r in avail]

    # 收集每天的 nextday 結果
    daily_results = []
    for d in avail_dates[:lookback_days]:
        r = compute_nextday(d)
        if r["stats"]["total_limit_up"] > 0 and r["next_date"]:
            r["_date"] = d
            daily_results.append(r)

    daily_results.reverse()  # 舊→新

    if not daily_results:
        return {"dates": [], "trend": [], "heatmap": {}, "theme_table": [], "stock_table": []}

    dates = [str(r["_date"]) for r in daily_results]

    # --- 1. 趨勢資料 ---
    trend = []
    for r in daily_results:
        st = r["stats"]
        trend.append({
            "date": str(r["_date"]),
            "date_short": r["_date"].strftime("%m/%d"),
            "total": st["total_limit_up"],
            "continuation": st["continuation_count"],
            "open_return": st["open_return"],
            "avg_return": st["avg_return"],
            "close_return": st["close_return"],
            "open_positive_rate": st["open_positive_rate"],
            "close_positive_rate": st["close_positive_rate"],
        })

    # --- 2. 族群熱力圖 + 族群統計 ---
    # {theme: [{date, close_return, count}, ...]}
    heatmap = {}
    theme_agg = {}  # {theme: {days, total_count, open_rets, avg_rets, close_rets}}

    for r in daily_results:
        d_str = str(r["_date"])
        for ts in r.get("theme_stats", []):
            th = ts["theme"]
            if th not in heatmap:
                heatmap[th] = []
            heatmap[th].append({
                "date": d_str,
                "date_short": r["_date"].strftime("%m/%d"),
                "close_return": ts["avg_close_return"],
                "count": ts["count"],
            })
            if th not in theme_agg:
                theme_agg[th] = {"days": 0, "total_count": 0,
                                 "open_rets": [], "avg_rets": [], "close_rets": []}
            theme_agg[th]["days"] += 1
            theme_agg[th]["total_count"] += ts["count"]
            theme_agg[th]["open_rets"].append(ts["avg_open_return"])
            theme_agg[th]["avg_rets"].append((ts["avg_open_return"] + ts["avg_close_return"]) / 2)
            theme_agg[th]["close_rets"].append(ts["avg_close_return"])

    theme_table = []
    for th, agg in sorted(theme_agg.items(), key=lambda x: x[1]["total_count"], reverse=True):
        n = len(agg["open_rets"])
        theme_table.append({
            "theme": th,
            "days": agg["days"],
            "count": agg["total_count"],
            "open_return": round(sum(agg["open_rets"]) / n, 2) if n else 0,
            "avg_return": round(sum(agg["avg_rets"]) / n, 2) if n else 0,
            "close_return": round(sum(agg["close_rets"]) / n, 2) if n else 0,
        })

    # --- 3. 個股績效表 ---
    stock_agg = {}  # {code: {name, theme, days, open_rets, avg_rets, close_rets}}
    for r in daily_results:
        for s in r.get("stocks", []):
            code = s["code"]
            if code not in stock_agg:
                stock_agg[code] = {"name": s["name"], "theme": s.get("theme", ""),
                                   "days": 0, "open_rets": [], "avg_rets": [], "close_rets": []}
            stock_agg[code]["days"] += 1
            stock_agg[code]["open_rets"].append(s["open_return"])
            stock_agg[code]["avg_rets"].append(s["avg_return"])
            stock_agg[code]["close_rets"].append(s["close_return"])

    stock_table = []
    for code, agg in sorted(stock_agg.items(), key=lambda x: x[1]["days"], reverse=True):
        n = len(agg["open_rets"])
        stock_table.append({
            "code": code,
            "name": agg["name"],
            "theme": agg["theme"],
            "days": agg["days"],
            "count": n,
            "open_return": round(sum(agg["open_rets"]) / n, 2) if n else 0,
            "avg_return": round(sum(agg["avg_rets"]) / n, 2) if n else 0,
            "close_return": round(sum(agg["close_rets"]) / n, 2) if n else 0,
        })

    return {
        "dates": dates,
        "trend": trend,
        "heatmap": heatmap,
        "theme_table": theme_table,
        "stock_table": stock_table,
    }


if __name__ == "__main__":
    result = compute_stats(datetime.date(2026, 3, 20))
    print(f"日期: {result['dates']}")
    print(f"\n趨勢:")
    for t in result["trend"]:
        print(f"  {t['date_short']} 漲停{t['total']} 續{t['continuation']} 開{t['open_return']:+.2f}% 收{t['close_return']:+.2f}%")
    print(f"\n族群 ({len(result['theme_table'])}):")
    for t in result["theme_table"][:10]:
        print(f"  {t['theme']:15s} {t['days']}天 {t['count']}股 開{t['open_return']:+.2f}% 收{t['close_return']:+.2f}%")
    print(f"\n個股 ({len(result['stock_table'])}):")
    for s in result["stock_table"][:10]:
        print(f"  {s['code']} {s['name']:8s} {s['days']}天 開{s['open_return']:+.2f}% 收{s['close_return']:+.2f}%")
