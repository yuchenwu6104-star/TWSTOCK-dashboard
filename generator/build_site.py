#!/usr/bin/env python3
"""靜態網站生成器：從 DuckDB 讀資料，用 Jinja2 生成 HTML。"""

import duckdb
import datetime
import os
import shutil
import sys

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH
from crawler.concept_classifier import AI_CONCEPT_OVERLAY, SECTOR_ICON_MAP
from crawler.disposal_forecast import compute_forecast
from crawler.broker_data import get_top_brokers
from crawler.nextday_performance import compute_nextday, compute_history

SUPP_DB = "/Users/slking/taiwan_stock_dashboard/收盤觀察/market_supplementary.duckdb"


def _get_institutional(stock_code: str, trade_date) -> dict | None:
    import os
    if not os.path.exists(SUPP_DB):
        return None
    try:
        con = duckdb.connect(SUPP_DB, read_only=True)
        r = con.execute("""
            SELECT foreign_net, sitc_net, dealer_net, total_net, trade_date
            FROM institutional_daily
            WHERE symbol=? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
        """, [stock_code, trade_date]).fetchone()
        con.close()
        if r:
            return {"foreign_net": r[0] or 0, "sitc_net": r[1] or 0,
                    "dealer_net": r[2] or 0, "total_net": r[3] or 0,
                    "data_date": r[4].strftime("%m/%d") if r[4] else ""}
    except Exception:
        pass
    return None


def _get_margin(stock_code: str, trade_date) -> dict | None:
    import os
    if not os.path.exists(SUPP_DB):
        return None
    try:
        con = duckdb.connect(SUPP_DB, read_only=True)
        r = con.execute("""
            SELECT margin_buy, margin_sell, margin_bal, margin_prev_bal,
                   short_sell, short_buy, short_bal, short_prev_bal, trade_date
            FROM margin_daily
            WHERE symbol=? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 1
        """, [stock_code, trade_date]).fetchone()
        con.close()
        if r:
            margin_change = (r[2] or 0) - (r[3] or 0)
            short_change = (r[6] or 0) - (r[7] or 0)
            ratio = round((r[6] or 0) / r[2] * 100, 1) if r[2] and r[2] > 0 else 0
            return {
                "margin_buy": r[0] or 0, "margin_sell": r[1] or 0,
                "margin_bal": r[2] or 0, "margin_change": margin_change,
                "short_bal": r[6] or 0, "short_change": short_change,
                "ratio": ratio,
                "data_date": r[8].strftime("%m/%d") if r[8] else "",
            }
    except Exception:
        pass
    return None


def _build_bubble_datasets(buy_top: list, sell_top: list, close_price: float) -> tuple:
    """Build 2 datasets for bubble chart: X=買進張數, Y=賣出張數, r=淨量.

    對角線以下 = 買超（紅），以上 = 賣超（綠）。
    一眼看出每家券商買多少、賣多少。
    """
    import math

    def mk(b, is_buyer: bool):
        net = b["net_lots"] if is_buyer else -b["net_lots"]
        r = round(max(4, min(30, math.sqrt(abs(b["net_lots"])) * 1.2)), 1)
        return {
            "x": int(b["buy_lots"]),
            "y": int(b["sell_lots"]),
            "r": r,
            "label": b["name"],
            "net": int(net),
            "buyVol": int(b["buy_lots"]),
            "sellVol": int(b["sell_lots"]),
            "buyAvg": round(float(b["buy_avg"]), 2),
            "sellAvg": round(float(b["sell_avg"]), 2),
        }

    buy_data = [mk(b, True) for b in buy_top]
    sell_data = [mk(b, False) for b in sell_top]

    return buy_data, sell_data

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "docs")
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

ARCHIVE_DAYS = 180

# 處置門檻條件（靜態資料）
DISPOSAL_CONDITIONS = [
    {"name": "3①", "description": "連續3個營業日經第1款公布注意交易資訊即處置"},
    {"name": "5", "description": "連續5個營業日經第1~8款公布注意交易資訊即處置"},
    {"name": "6", "description": "最近10個營業日內有6日經第1~8款公布注意交易資訊即處置"},
    {"name": "12", "description": "最近30個營業日內有12日經第1~8款公布注意交易資訊即處置"},
]

TRIGGER_CONDITIONS = [
    {"name": "第1款", "description": "漲幅/跌幅/週轉率異常"},
    {"name": "第2款", "description": "本益比/股價淨值比異常"},
    {"name": "第3款", "description": "交易量異常"},
    {"name": "第4款", "description": "週轉率異常"},
    {"name": "第5款", "description": "單一券商買賣占比過高"},
    {"name": "第6款", "description": "本益比/股價淨值比/週轉率/集中度綜合"},
    {"name": "第7款", "description": "券資比異常放大"},
    {"name": "第8款", "description": "TDR溢折價異常"},
]


def _get_theme_icon(theme_name: str) -> str:
    """查找族群 icon。"""
    if theme_name in AI_CONCEPT_OVERLAY:
        return AI_CONCEPT_OVERLAY[theme_name]["icon"]
    return SECTOR_ICON_MAP.get(theme_name, "📊")


def _get_available_dates(con) -> list[str]:
    """取得所有有資料的日期（降序）。"""
    rows = con.execute("""
        SELECT DISTINCT date FROM daily_theme ORDER BY date DESC
    """).fetchall()
    return [r[0].strftime("%Y%m%d") for r in rows]


def _last_trading_day(d: datetime.date = None) -> datetime.date:
    if d is None:
        d = datetime.date.today()
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d


def build(trade_date: datetime.date = None):
    if trade_date is None:
        trade_date = _last_trading_day()

    date_str = trade_date.strftime("%Y-%m-%d")
    date_compact = trade_date.strftime("%Y%m%d")

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    con = duckdb.connect(DB_PATH, read_only=True)

    # ---------- 可用日期（供 ◀▶ 導航） ----------
    avail_dates = _get_available_dates(con)
    prev_date = None
    next_date = None
    if date_compact in avail_dates:
        idx = avail_dates.index(date_compact)
        if idx < len(avail_dates) - 1:
            prev_date = avail_dates[idx + 1]
        if idx > 0:
            next_date = avail_dates[idx - 1]

    # ---------- 讀族群資料 ----------
    themes_raw = con.execute("""
        SELECT theme_name, theme_summary, key_driver, stock_list, stock_count
        FROM daily_theme
        WHERE date = ?
        ORDER BY stock_count DESC
    """, [trade_date]).fetchall()

    if not themes_raw:
        print(f"⚠ {date_str} 無族群資料，跳過生成。")
        con.close()
        return

    # 讀漲停股詳情
    prices = con.execute("""
        SELECT stock_code, stock_name, close_price, change_pct, volume,
               trade_value, market, change_price
        FROM daily_price
        WHERE date = ? AND is_limit_up = true
    """, [trade_date]).fetchall()
    price_map = {}
    for r in prices:
        vol = int(r[4])
        vol_lots = vol // 1000 if vol >= 1000 else vol  # 轉張數
        price_map[r[0]] = {
            "code": r[0], "name": r[1], "close_price": float(r[2]),
            "change_pct": f"{float(r[3]):.2f}", "volume": vol,
            "trade_value": int(r[5]), "market": r[6],
            "change": f"{float(r[7]):.2f}" if r[7] else "0",
            "volume_str": f"{vol_lots:,}",
            "reason": "",  # 將由 LLM 填入
        }

    # 讀 meta
    metas = con.execute("SELECT stock_code, industry, sector_group FROM stock_meta").fetchall()
    meta_map = {r[0]: {"industry": r[1] or "", "sector_group": r[2] or ""} for r in metas}

    # 讀個股漲停原因
    reason_map = {}
    try:
        reasons = con.execute("SELECT stock_code, reason FROM stock_reason WHERE date = ?",
                              [trade_date]).fetchall()
        reason_map = {r[0]: r[1] for r in reasons}
    except Exception:
        pass  # 表可能不存在

    # 填入 reason
    for code, stock in price_map.items():
        stock["reason"] = reason_map.get(code, "")

    con.close()

    # 組裝 themes
    themes = {}
    total_limit_up = 0
    for row in themes_raw:
        theme_name, summary, driver, stock_codes, count = row
        stocks = []
        for code in stock_codes:
            s = price_map.get(code)
            if s:
                stocks.append(s)
        themes[theme_name] = {
            "summary": summary,
            "driver": driver,
            "stocks": stocks,
            "icon": _get_theme_icon(theme_name),
        }
        total_limit_up += len(stocks)

    # ---------- 確保目錄 ----------
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ---------- 1. 首頁 (index.html) ----------
    tpl_index = env.get_template("index.html")

    # 整理 stock data for template (用 price, change, change_pct, volume_str)
    for theme_name, theme in themes.items():
        for s in theme["stocks"]:
            s["price"] = s["close_price"]

    html = tpl_index.render(
        date=trade_date.strftime("%Y/%m/%d"),
        date_compact=date_compact,
        limit_up_count=total_limit_up,
        themes=themes,
        prev_date=prev_date,
        next_date=next_date,
        available_dates=avail_dates,
    )
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ index.html ({total_limit_up} 檔漲停, {len(themes)} 族群)")

    # ---------- 2. 個股頁 ----------
    stock_dir = os.path.join(OUTPUT_DIR, "stock", date_compact)
    os.makedirs(stock_dir, exist_ok=True)

    tpl_stock = env.get_template("stock.html")
    code_to_theme = {}
    for theme_name, theme in themes.items():
        for s in theme["stocks"]:
            code_to_theme[s["code"]] = theme_name

    for code, stock in price_map.items():
        meta = meta_map.get(code, {})
        stock_data = {
            **stock,
            "theme": code_to_theme.get(code, ""),
            "industry": meta.get("industry", ""),
            "sector_group": meta.get("sector_group", ""),
        }
        # 券商分點
        try:
            buy_top, sell_top = get_top_brokers(code, trade_date, top_n=15)
        except Exception:
            buy_top, sell_top = [], []

        broker_count = 0
        try:
            _con = duckdb.connect(DB_PATH, read_only=True)
            row = _con.execute("SELECT COUNT(DISTINCT broker_id) FROM broker_rank WHERE date=? AND stock_code=?",
                               [trade_date, code]).fetchone()
            broker_count = row[0] if row else 0
            _con.close()
        except Exception:
            pass

        # Institutional + margin
        inst = _get_institutional(code, trade_date)
        margin = _get_margin(code, trade_date)

        # Bubble datasets (2 groups: buy_data, sell_data)
        close_p = stock.get("close_price", 0)
        buy_bubble, sell_bubble = _build_bubble_datasets(buy_top, sell_top, close_p)

        import json as _json
        html = tpl_stock.render(
            date=trade_date.strftime("%Y/%m/%d"), date_compact=date_compact,
            stock=stock_data,
            buy_top=buy_top, sell_top=sell_top, broker_count=broker_count,
            inst=inst, margin=margin,
            buy_bubble_json=_json.dumps(buy_bubble, ensure_ascii=False),
            sell_bubble_json=_json.dumps(sell_bubble, ensure_ascii=False),
        )
        with open(os.path.join(stock_dir, f"{code}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    print(f"✓ 個股頁: {len(price_map)} 個")

    # ---------- 3. 隔日表現 (nextday-performance.html) ----------
    # 用前一個交易日的漲停來算（因為今天的隔日還沒出來）
    # 找最近一個有隔日資料的漲停日
    tpl_nextday = env.get_template("nextday-performance.html")

    _prev_dates = _get_available_dates(duckdb.connect(DB_PATH, read_only=True))
    # 排除當天（當天的隔日還沒收盤）
    _prev_limit_up_dates = [d for d in _prev_dates if d < date_compact]

    nextday_result = None
    nextday_limit_up_date = ""
    for pdate in _prev_limit_up_dates:
        d_obj = datetime.datetime.strptime(pdate, "%Y%m%d").date()
        r = compute_nextday(d_obj)
        if r["stats"]["total_limit_up"] > 0 and r["next_date"]:
            nextday_result = r
            nextday_limit_up_date = pdate
            break

    if nextday_result:
        import json as _json2
        history = compute_history(trade_date, lookback_days=10)
        # 加 template 需要的 alias
        for s in nextday_result["stocks"]:
            s["next_return"] = s.get("close_return", 0)
        html = tpl_nextday.render(
            date=trade_date.strftime("%Y/%m/%d"), date_compact=date_compact,
            limit_up_date=nextday_result["limit_up_date"],
            next_date=nextday_result["next_date"],
            stats=nextday_result["stats"],
            stocks=nextday_result["stocks"],
            theme_stats=nextday_result["theme_stats"],
            history_json=_json2.dumps(history, ensure_ascii=False),
        )
        print(f"✓ nextday-performance.html ({nextday_result['stats']['total_limit_up']}檔 → {nextday_result['next_date']})")
    else:
        html = tpl_nextday.render(
            date=trade_date.strftime("%Y/%m/%d"), date_compact=date_compact,
            limit_up_date="", next_date="",
            stats={"open_return": 0, "open_positive_rate": 0, "avg_return": 0,
                   "avg_positive_rate": 0, "close_return": 0, "close_positive_rate": 0,
                   "continuation_count": 0, "total_limit_up": 0},
            stocks=[], theme_stats=[], history_json="[]",
        )
        print(f"✓ nextday-performance.html (無資料)")

    with open(os.path.join(OUTPUT_DIR, "nextday-performance.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # ---------- 4. 處置標準 (disposal-forecast.html) ----------
    tpl_disposal = env.get_template("disposal-forecast.html")
    forecast = compute_forecast(trade_date)

    disposal_stats = {
        "almost": len(forecast["almost"]),
        "two_more": len(forecast["two_more"]),
        "in_disposal": len(forecast["in_disposal"]),
    }
    # 轉格式給模板
    almost_stocks = [
        {"code": s["symbol"], "name": s["name"], "market": s.get("exchange", "TWSE"),
         "rule": s["rule_id"], "count": s["count"], "threshold": s["threshold"]}
        for s in forecast["almost"]
    ]
    two_more_stocks = [
        {"code": s["symbol"], "name": s["name"], "market": s.get("exchange", "TWSE"),
         "rule": s["rule_id"], "count": s["count"], "threshold": s["threshold"]}
        for s in forecast["two_more"]
    ]
    in_disposal_stocks = [
        {"code": s["symbol"], "name": s["name"],
         "exchange": s.get("exchange", "TWSE"),
         "level": s.get("level", 1),
         "start_date": s["start_date"], "end_date": s["end_date"],
         "interval": s["interval"],
         "rule_group": s["rule_group"]}
        for s in forecast["in_disposal"]
    ]

    html = tpl_disposal.render(
        date=date_str, date_compact=date_compact,
        applicable_date=forecast.get("applicable_date", ""),
        stats=disposal_stats,
        almost_stocks=almost_stocks,
        two_more_stocks=two_more_stocks,
        in_disposal_stocks=in_disposal_stocks,
        conditions=TRIGGER_CONDITIONS,
    )
    with open(os.path.join(OUTPUT_DIR, "disposal-forecast.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ disposal-forecast.html ({disposal_stats['almost']} 差1次, {disposal_stats['in_disposal']} 處置中)")

    # ---------- 清理舊頁 ----------
    cleanup_old_pages(trade_date)

    print(f"✅ 網站生成完成 → {OUTPUT_DIR}")


def cleanup_old_pages(current_date: datetime.date):
    """刪除超過 ARCHIVE_DAYS 的個股頁目錄。"""
    daily_dir = os.path.join(OUTPUT_DIR, "stock")
    if not os.path.exists(daily_dir):
        return
    cutoff = current_date - datetime.timedelta(days=ARCHIVE_DAYS)
    removed = 0
    for name in os.listdir(daily_dir):
        try:
            d = datetime.datetime.strptime(name, "%Y%m%d").date()
            if d < cutoff:
                shutil.rmtree(os.path.join(daily_dir, name))
                removed += 1
        except ValueError:
            continue
    if removed:
        print(f"🗑 清除 {removed} 個過期日期目錄（>{ARCHIVE_DAYS} 天）")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    args = parser.parse_args()
    if args.date:
        d = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        d = None
    build(d)
