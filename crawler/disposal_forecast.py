#!/usr/bin/env python3
"""處置預測模組 v2：完整 4 門檻進度 + 觸發價計算 + 近期注意紀錄。"""

import duckdb
import datetime
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.paths import DB_PATH, DISP_DB, MARKET_DB
from crawler.twse_api import _calc_limit_up

# 處置門檻定義
THRESHOLDS = [
    {"id": "3①", "window": 3, "need": 3, "consecutive": True, "rule_filter": "first_only",
     "desc": "連續3個營業日經第1款公布注意交易資訊即處置"},
    {"id": "5", "window": 5, "need": 5, "consecutive": True, "rule_filter": "any",
     "desc": "連續5個營業日經第1~8款公布注意交易資訊即處置"},
    {"id": "6/10", "window": 10, "need": 6, "consecutive": False, "rule_filter": "any",
     "desc": "最近10個營業日內有6日經第1~8款公布注意交易資訊即處置"},
    {"id": "12/30", "window": 30, "need": 12, "consecutive": False, "rule_filter": "any",
     "desc": "最近30個營業日內有12日經第1~8款公布注意交易資訊即處置"},
]


def _is_common_stock(symbol: str) -> bool:
    return len(symbol) == 4 and symbol.isdigit()


def _has_first_rule(rule_code: str) -> bool:
    if not rule_code:
        return False
    return "第一款" in rule_code


def _get_trading_days(con, end_date: datetime.date, lookback: int = 35) -> list[datetime.date]:
    rows = con.execute("""
        SELECT DISTINCT announcement_date
        FROM attention_events
        WHERE announcement_date <= ?
        ORDER BY announcement_date DESC
        LIMIT ?
    """, [end_date, lookback]).fetchall()
    return [r[0] for r in rows]


# ---- 第1款觸發價計算 ----

def _rule1_trigger_price(start_price: float, exchange: str) -> float:
    """計算第1款觸發價。

    TWSE 規則（6日累積漲跌幅門檻）：
      價差 < 3 元 → 不適用（豁免）
      價差 < 40 元 → 30%
      價差 < 50 元 → 32%
      價差 ≥ 50 元 → 23% + 40 元
    TPEx 規則：
      價差 < 3 元 → 不適用
      價差 < 50 元 → 25%
      價差 ≥ 50 元 → 25% + 50 元
    """
    if start_price <= 0:
        return 0

    if exchange == "TPEx":
        # 先試 25%
        trigger = start_price * 1.25
        diff = trigger - start_price
        if diff < 3:
            return 0  # 豁免
        if diff < 50:
            return round(trigger, 2)
        # ≥ 50 → 25% + 50 元
        return round(start_price + 50 + start_price * 0.25, 2)
    else:
        # TWSE: 試不同級距
        # 30% 門檻
        trigger_30 = start_price * 1.30
        diff_30 = trigger_30 - start_price
        if diff_30 < 3:
            return 0
        if diff_30 < 40:
            return round(trigger_30, 2)
        # 32% 門檻
        trigger_32 = start_price * 1.32
        diff_32 = trigger_32 - start_price
        if diff_32 < 50:
            return round(trigger_32, 2)
        # 23% + 40 元
        return round(start_price * 1.23 + 40, 2)


def _rule1_threshold_desc(start_price: float, exchange: str) -> str:
    """回傳第1款適用的門檻描述。"""
    if start_price <= 0:
        return ""
    if exchange == "TPEx":
        diff = start_price * 0.25
        if diff < 3:
            return "豁免（價差<3元）"
        if diff < 50:
            return "須達 25% 門檻"
        return "適用 25%+50元 門檻"
    else:
        diff_30 = start_price * 0.30
        if diff_30 < 3:
            return "豁免（價差<3元）"
        if diff_30 < 40:
            return "須達 30% 門檻"
        diff_32 = start_price * 0.32
        if diff_32 < 50:
            return "須達 32% 門檻"
        return "適用 23%+40元 門檻"


# _calc_limit_up and _calc_limit_down imported from crawler.twse_api
from crawler.twse_api import _calc_limit_down


# ---- 股本資料 ----

_shares_cache = {}

def _get_issued_shares(symbol: str) -> int:
    """從 security_reference 取發行股數（有快取）。"""
    if symbol in _shares_cache:
        return _shares_cache[symbol]
    if not _shares_cache:
        # 一次全讀
        try:
            con = duckdb.connect(DISP_DB, read_only=True)
            rows = con.execute("SELECT symbol, issued_common_shares FROM security_reference WHERE issued_common_shares > 0").fetchall()
            con.close()
            for r in rows:
                _shares_cache[r[0]] = int(r[1])
        except Exception:
            pass
    return _shares_cache.get(symbol, 0)


# ---- 取市場資料 ----

def _get_market_data(symbols: list[str], as_of_date: datetime.date) -> dict:
    """從 market_daily.duckdb 取收盤價、成交量、6日前收盤。
    回傳 {symbol: {close, prev_close, start_6d, volume, avg_volume_60d, turnover_rate, ...}}
    """
    if not os.path.exists(MARKET_DB):
        return {}

    con = duckdb.connect(MARKET_DB, read_only=True)
    result = {}

    for sym in symbols:
        # 最近 65 個交易日的 OHLCV
        rows = con.execute("""
            SELECT trade_date, close, volume, trade_value
            FROM daily_ohlcv
            WHERE symbol = ? AND trade_date <= ?
            ORDER BY trade_date DESC
            LIMIT 65
        """, [sym, as_of_date]).fetchall()

        if not rows:
            continue

        close = float(rows[0][1]) if rows[0][1] else 0
        volume = int(rows[0][2]) if rows[0][2] else 0

        # 6 日前收盤（第 6 筆，index=5）
        start_6d = float(rows[5][1]) if len(rows) > 5 and rows[5][1] else 0

        # 60 日均量
        volumes_60 = [int(r[2]) for r in rows[:60] if r[2]]
        avg_vol_60 = sum(volumes_60) / len(volumes_60) if volumes_60 else 0

        # 6 日累積漲幅
        accum_pct_6d = round((close / start_6d - 1) * 100, 2) if start_6d > 0 else 0

        # 30 日累積漲幅
        start_30d = float(rows[29][1]) if len(rows) > 29 and rows[29][1] else 0
        accum_pct_30d = round((close / start_30d - 1) * 100, 2) if start_30d > 0 else 0

        # 前一日收盤（算漲停用）
        prev_close = float(rows[1][1]) if len(rows) > 1 and rows[1][1] else 0

        result[sym] = {
            "close": close,
            "prev_close": prev_close,
            "start_6d": start_6d,
            "accum_pct_6d": accum_pct_6d,
            "accum_pct_30d": accum_pct_30d,
            "volume": volume,
            "volume_lots": volume // 1000,
            "avg_volume_60d": avg_vol_60,
            "avg_volume_60d_lots": int(avg_vol_60) // 1000,
            "volume_ratio": round(volume / avg_vol_60, 2) if avg_vol_60 > 0 else 0,
            "limit_up": _calc_limit_up(prev_close),
            "limit_down": _calc_limit_down(prev_close),
        }

    con.close()
    return result


# ---- 主函數 ----

def compute_forecast(as_of_date: datetime.date = None) -> dict:
    """計算處置預測 v2。

    回傳每支股票的完整資料：
    - 全部 4 門檻進度
    - 近期注意紀錄（按日+款項）
    - 第 1 款觸發價計算
    - 收盤價/漲停價/跌停價
    """
    if not os.path.exists(DISP_DB):
        print(f"⚠ disposition DB 不存在: {DISP_DB}")
        return {"in_disposal": [], "almost": [], "two_more": [], "stocks": {}}

    con = duckdb.connect(DISP_DB, read_only=True)

    if as_of_date is None:
        row = con.execute("SELECT MAX(announcement_date) FROM attention_events").fetchone()
        as_of_date = row[0] if row[0] else datetime.date.today()

    # --- 1. 處置中 ---
    in_disposal = []
    check_date = as_of_date  # 歷史頁用 as_of_date，最新頁由呼叫端傳今天
    disp_rows = con.execute("""
        SELECT symbol, name, disposal_level, start_date, end_date,
               rule_group, matching_interval_seconds, exchange
        FROM disposal_events
        WHERE end_date >= ? AND start_date <= ?
        ORDER BY disposal_level DESC, symbol
    """, [check_date, check_date]).fetchall()

    # 去重：同股多筆處置保留最高 level
    in_disposal_dedup = {}
    for r in disp_rows:
        sym = r[0]
        if not _is_common_stock(sym):
            continue
        level = r[2] or 0
        interval_min = r[6] // 60 if r[6] else 5
        entry = {
            "symbol": sym, "name": r[1], "level": level,
            "start_date": str(r[3]), "end_date": str(r[4]),
            "rule_group": r[5] or "",
            "interval": f"{interval_min}分鐘",
            "exchange": r[7] or "TWSE",
        }
        if sym not in in_disposal_dedup or level > (in_disposal_dedup[sym]["level"] or 0):
            in_disposal_dedup[sym] = entry

    in_disposal = sorted(in_disposal_dedup.values(), key=lambda x: (-(x["level"] or 0), x["symbol"]))
    in_disposal_symbols = set(in_disposal_dedup.keys())

    # --- 2. 營業日 ---
    trading_days = _get_trading_days(con, as_of_date, 35)
    if len(trading_days) < 3:
        con.close()
        return {"as_of_date": str(as_of_date), "in_disposal": in_disposal,
                "almost": [], "two_more": [], "stocks": {}}

    # --- 3. 近 30 日 attention events ---
    window_start = trading_days[min(len(trading_days) - 1, 29)]
    attn_rows = con.execute("""
        SELECT symbol, name, announcement_date, rule_code, exchange
        FROM attention_events
        WHERE announcement_date >= ? AND announcement_date <= ?
        ORDER BY symbol, announcement_date
    """, [window_start, as_of_date]).fetchall()

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

    # --- 3b. 查歷史處置次數（用於推算最快處置級別）---
    prior_disposals = {}
    all_attn_symbols = list(stock_attn.keys())
    if all_attn_symbols:
        try:
            placeholders = ",".join(["?"] * len(all_attn_symbols))
            rows_pd = con.execute(f"""
                SELECT symbol, COUNT(*) as cnt
                FROM disposal_events
                WHERE symbol IN ({placeholders}) AND end_date < ?
                GROUP BY symbol
            """, all_attn_symbols + [as_of_date]).fetchall()
            for r in rows_pd:
                prior_disposals[r[0]] = r[1]
        except Exception:
            pass

    con.close()

    # --- 4. 取市場資料 ---
    all_symbols = list(stock_attn.keys())
    market_data = _get_market_data(all_symbols, as_of_date)

    # --- 5. 每支股票算全部門檻 ---
    almost = []
    two_more = []
    stocks_detail = {}  # {symbol: 完整明細}

    for sym, info in stock_attn.items():
        if sym in in_disposal_symbols:
            continue

        mkt = market_data.get(sym, {})
        exchange = info.get("exchange", "TWSE")

        # 全部 4 門檻進度
        thresholds_progress = []
        closest_gap = 999
        closest_threshold = None

        for th in THRESHOLDS:
            window = th["window"]
            need = th["need"]
            window_days = trading_days[:window] if len(trading_days) >= window else trading_days

            if th["consecutive"]:
                count = 0
                for d in window_days:
                    if d in info["dates"]:
                        if th["rule_filter"] == "first_only":
                            if any(_has_first_rule(rc) for rc in info["dates"][d]):
                                count += 1
                            else:
                                break
                        else:
                            count += 1
                    else:
                        break
            else:
                count = 0
                for d in window_days:
                    if d in info["dates"]:
                        if th["rule_filter"] == "first_only":
                            if any(_has_first_rule(rc) for rc in info["dates"][d]):
                                count += 1
                        else:
                            count += 1

            gap = need - count
            thresholds_progress.append({
                "id": th["id"], "count": count, "need": need,
                "gap": gap, "desc": th["desc"],
            })

            if 0 < gap < closest_gap:
                closest_gap = gap
                closest_threshold = th["id"]

            if gap == 1:
                almost.append({
                    "symbol": sym, "name": info["name"],
                    "exchange": exchange,
                    "count": count, "threshold": need,
                    "rule_id": th["id"], "rule_desc": th["desc"],
                })
            elif gap == 2:
                two_more.append({
                    "symbol": sym, "name": info["name"],
                    "exchange": exchange,
                    "count": count, "threshold": need,
                    "rule_id": th["id"], "rule_desc": th["desc"],
                })

        # 近期注意紀錄（按日期排）
        recent_notices = []
        sorted_dates = sorted(info["dates"].keys())
        for dt in sorted_dates:
            rules = info["dates"][dt]
            # 合併同日 rule_codes
            all_rules = set()
            for rc in rules:
                for part in rc.split(","):
                    part = part.strip()
                    if part:
                        all_rules.add(part)
            recent_notices.append({
                "date": dt.strftime("%m/%d"),
                "rules": sorted(all_rules),
            })

        # 觸發價計算
        trigger_info = {}
        start_6d = mkt.get("start_6d", 0)
        close = mkt.get("close", 0)
        limit_up = mkt.get("limit_up", 0)
        limit_down = mkt.get("limit_down", 0)

        if start_6d > 0:
            trigger_price = _rule1_trigger_price(start_6d, exchange)
            threshold_desc = _rule1_threshold_desc(start_6d, exchange)
            accum = mkt.get("accum_pct_6d", 0)
            diff = round(trigger_price - start_6d, 2) if trigger_price else 0

            # 判斷觸發難度
            if trigger_price <= 0:
                status = "exempt"
                status_text = "豁免（價差不足）"
            elif trigger_price <= limit_down:
                status = "certain"
                status_text = "無論漲跌皆符合"
            elif trigger_price <= close:
                status = "certain"
                status_text = "無論漲跌皆符合"
            elif trigger_price <= limit_up:
                pct_needed = round((trigger_price / close - 1) * 100, 1)
                status = "possible"
                status_text = f"{trigger_price} ↑(含) (+{pct_needed}%)"
            else:
                status = "impossible"
                status_text = "超過漲停，不可能觸發"

            trigger_info = {
                "start_6d": start_6d,
                "trigger_price": trigger_price,
                "threshold_desc": threshold_desc,
                "accum_pct": accum,
                "diff": diff,
                "status": status,
                "status_text": status_text,
            }

        # 第3款（量）、第4款（週轉率）附加條件
        rule3_info = {}
        rule4_info = {}
        if mkt:
            avg_vol_lots = mkt.get("avg_volume_60d_lots", 0)
            vol_ratio = mkt.get("volume_ratio", 0)
            rule3_info = {
                "need_volume_lots": avg_vol_lots,
                "current_ratio": vol_ratio,
            }
            # 週轉率 = 當日成交量 / 發行股數 * 100
            shares = _get_issued_shares(sym)
            current_turnover = round(mkt.get("volume", 0) / shares * 100, 2) if shares > 0 else 0
            rule4_info = {
                "need_turnover": 10.0,
                "current_turnover": current_turnover,
            }

        # 最快處置預估
        earliest_disposal_label = ""
        if closest_gap < 999:
            # 每補一次注意需 1 個交易日，處置再多 1 日才生效
            earliest_date = _nth_next_trading_day(as_of_date, closest_gap + 1)
            prior_count = prior_disposals.get(sym, 0)
            next_level = prior_count + 1
            interval_min = 5 if next_level == 1 else 20
            earliest_disposal_label = (
                f"{earliest_date.strftime('%m-%d')} "
                f"第{next_level}次{interval_min}分"
            )

        stocks_detail[sym] = {
            "symbol": sym,
            "name": info["name"],
            "exchange": exchange,
            "close": close,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "thresholds": thresholds_progress,
            "closest_threshold": closest_threshold,
            "closest_gap": closest_gap,
            "recent_notices": recent_notices,
            "trigger": trigger_info,
            "rule3": rule3_info,
            "rule4": rule4_info,
            "accum_pct_30d": mkt.get("accum_pct_30d", 0),
            "earliest_disposal": earliest_disposal_label,
        }

    # 去重
    almost_dedup = _dedup_closest(almost)
    two_more_dedup = _dedup_closest(two_more)

    # 適用日期
    applicable = as_of_date + datetime.timedelta(days=1)
    while applicable.weekday() >= 5:
        applicable += datetime.timedelta(days=1)

    return {
        "as_of_date": str(as_of_date),
        "applicable_date": str(applicable),
        "in_disposal": in_disposal,
        "almost": sorted(almost_dedup, key=lambda x: x["threshold"] - x["count"]),
        "two_more": sorted(two_more_dedup, key=lambda x: x["threshold"] - x["count"]),
        "stocks": stocks_detail,
    }


def _nth_next_trading_day(from_date: datetime.date, n: int) -> datetime.date:
    """從 from_date 算起，第 n 個交易日（跳過週六日）。"""
    d = from_date
    while n > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _dedup_closest(items: list[dict]) -> list[dict]:
    best = {}
    for item in items:
        sym = item["symbol"]
        gap = item["threshold"] - item["count"]
        if sym not in best or gap < best[sym]["threshold"] - best[sym]["count"]:
            best[sym] = item
    return list(best.values())


def sync_to_local_db(forecast: dict, db_path: str = DB_PATH):
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
    import json as _json
    result = compute_forecast()
    print(f"資料日期: {result['as_of_date']}")
    print(f"適用日期: {result['applicable_date']}")
    print(f"處置中: {len(result['in_disposal'])} 檔")
    print(f"差1次: {len(result['almost'])} 檔")
    print(f"差2次: {len(result['two_more'])} 檔")
    print(f"明細股數: {len(result['stocks'])} 檔")

    # 印差1次的詳細資料
    for s in result["almost"][:3]:
        sym = s["symbol"]
        detail = result["stocks"].get(sym, {})
        print(f"\n{'='*50}")
        print(f"{sym} {s['name']} ({s['exchange']}) 收盤${detail.get('close',0)}")
        print(f"  最接近門檻: {s['rule_id']} ({s['count']}/{s['threshold']})")
        for th in detail.get("thresholds", []):
            marker = "🔴" if th["gap"] == 1 else "🟡" if th["gap"] == 2 else "  "
            print(f"  {marker} {th['id']}: {th['count']}/{th['need']} (差{th['gap']}次)")
        trig = detail.get("trigger", {})
        if trig:
            print(f"  觸發價: {trig.get('status_text', '')} (起點${trig.get('start_6d',0)}, 累積{trig.get('accum_pct',0)}%)")
            print(f"  門檻: {trig.get('threshold_desc', '')}")
        notices = detail.get("recent_notices", [])
        if notices:
            print(f"  近期注意:")
            for n in notices[-5:]:
                print(f"    {n['date']}: {', '.join(n['rules'])}")
