#!/usr/bin/env python3
"""券商分點買賣超爬蟲 — 富邦看盤（上市+上櫃皆可，免驗證碼）。"""

import re
import time
import datetime
import duckdb
import requests

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH

FUBON_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco.djhtm"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _safe_int(val: str) -> int:
    try:
        return int(val.replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def fetch_broker_top15(stock_code: str) -> dict:
    """從富邦看盤抓買超/賣超前15名。

    回傳 {
        "data_date": "2026/03/20",
        "buy_top": [{"name", "buy_lots", "sell_lots", "net_lots", "pct"}],
        "sell_top": [同上],
        "total_buy_lots": int,
        "total_sell_lots": int,
        "avg_buy_cost": float,
        "avg_sell_cost": float,
    }
    """
    resp = requests.get(FUBON_URL, params={"a": stock_code}, headers=HEADERS, timeout=15)
    resp.encoding = "big5"
    text = resp.text

    # 取得更新日期
    date_match = re.search(r"最後更新日：(\d{4}/\d{2}/\d{2})", text)
    data_date = date_match.group(1) if date_match else ""

    # 提取所有 td 內容
    tds = []
    for m in re.finditer(r"<td[^>]*>(.*?)</td>", text, re.DOTALL | re.IGNORECASE):
        clean = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        tds.append(clean)

    # 找「買超券商」位置做起點
    try:
        start_idx = tds.index("買超券商")
    except ValueError:
        return {"data_date": data_date, "buy_top": [], "sell_top": [],
                "total_buy_lots": 0, "total_sell_lots": 0,
                "avg_buy_cost": 0, "avg_sell_cost": 0}

    # 跳過表頭（買超券商/買進/賣出/買超/佔比 + 賣超券商/買進/賣出/賣超/佔比 = 10 cells）
    data_start = start_idx + 10

    buy_top = []
    sell_top = []

    # 每行 10 cells：買超5(名/買/賣/超/比) + 賣超5(名/買/賣/超/比)
    i = data_start
    while i + 9 < len(tds):
        b_name = tds[i]
        b_buy = _safe_int(tds[i + 1])
        b_sell = _safe_int(tds[i + 2])
        b_net = _safe_int(tds[i + 3])
        b_pct = tds[i + 4]

        s_name = tds[i + 5]
        s_buy = _safe_int(tds[i + 6])
        s_sell = _safe_int(tds[i + 7])
        s_net = _safe_int(tds[i + 8])
        s_pct = tds[i + 9]

        # 碰到「合計」就結束
        if "合計" in b_name or "合計" in s_name:
            break

        if b_name and b_name not in ("買超券商",):
            buy_top.append({
                "name": b_name, "buy_lots": b_buy, "sell_lots": b_sell,
                "net_lots": b_net, "pct": b_pct,
                "buy_avg": 0, "sell_avg": 0,  # 富邦沒有分價位
            })
        if s_name and s_name not in ("賣超券商",):
            sell_top.append({
                "name": s_name, "buy_lots": s_buy, "sell_lots": s_sell,
                "net_lots": s_net, "pct": s_pct,
                "buy_avg": 0, "sell_avg": 0,
            })

        i += 10

    # 合計和平均成本
    total_buy = 0
    total_sell = 0
    avg_buy_cost = 0
    avg_sell_cost = 0
    for j in range(i, min(i + 20, len(tds))):
        if "合計買超" in tds[j] and j + 1 < len(tds):
            total_buy = _safe_int(tds[j + 1])
        if "合計賣超" in tds[j] and j + 1 < len(tds):
            total_sell = _safe_int(tds[j + 1])
        if "平均買超成本" in tds[j] and j + 1 < len(tds):
            try:
                avg_buy_cost = float(tds[j + 1].replace(",", ""))
            except ValueError:
                pass
        if "平均賣超成本" in tds[j] and j + 1 < len(tds):
            try:
                avg_sell_cost = float(tds[j + 1].replace(",", ""))
            except ValueError:
                pass

    # 填入平均成本當 buy_avg / sell_avg
    for b in buy_top:
        b["buy_avg"] = avg_buy_cost
        b["sell_avg"] = avg_sell_cost
    for s in sell_top:
        s["buy_avg"] = avg_buy_cost
        s["sell_avg"] = avg_sell_cost

    return {
        "data_date": data_date,
        "buy_top": buy_top,
        "sell_top": sell_top,
        "total_buy_lots": total_buy,
        "total_sell_lots": total_sell,
        "avg_buy_cost": avg_buy_cost,
        "avg_sell_cost": avg_sell_cost,
    }


def fetch_multiple_stocks(stock_codes: list[str], sleep_sec: float = 1.0) -> dict:
    """批次抓多支股票。回傳 {code: fetch_broker_top15 結果}。"""
    results = {}
    for code in stock_codes:
        try:
            data = fetch_broker_top15(code)
            if data["buy_top"] or data["sell_top"]:
                results[code] = data
                print(f"  [broker] {code}: 買超{len(data['buy_top'])}家 賣超{len(data['sell_top'])}家")
            else:
                print(f"  [broker] {code}: 無資料")
        except Exception as e:
            print(f"  [broker] {code}: 失敗 {e}")
        time.sleep(sleep_sec)
    return results


def save_broker_data(all_brokers: dict, trade_date: datetime.date, db_path: str = DB_PATH):
    """寫入 broker_rank 表。"""
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS broker_rank (
            date DATE, stock_code VARCHAR,
            broker_id VARCHAR, broker_name VARCHAR,
            buy_volume BIGINT, sell_volume BIGINT, net_volume BIGINT,
            buy_lots INTEGER, sell_lots INTEGER, net_lots INTEGER,
            buy_avg DECIMAL(10,2), sell_avg DECIMAL(10,2),
            PRIMARY KEY (date, stock_code, broker_id)
        )
    """)
    con.execute("DELETE FROM broker_rank WHERE date = ?", [trade_date])

    count = 0
    for stock_code, data in all_brokers.items():
        for side in ("buy_top", "sell_top"):
            for b in data[side]:
                broker_id = b["name"][:4]
                con.execute("INSERT OR REPLACE INTO broker_rank VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            [trade_date, stock_code, broker_id, b["name"],
                             b["buy_lots"] * 1000, b["sell_lots"] * 1000,
                             b["net_lots"] * 1000 if side == "buy_top" else -b["net_lots"] * 1000,
                             b["buy_lots"], b["sell_lots"],
                             b["net_lots"] if side == "buy_top" else -b["net_lots"],
                             b.get("buy_avg", 0), b.get("sell_avg", 0)])
                count += 1
    con.close()
    print(f"✓ broker_rank: {count} 筆")


def get_top_brokers(stock_code: str, trade_date: datetime.date,
                    top_n: int = 15, db_path: str = DB_PATH) -> tuple[list, list]:
    """從 DB 撈買超/賣超前 N 名。"""
    con = duckdb.connect(db_path, read_only=True)

    cols = [r[0] for r in con.execute("DESCRIBE broker_rank").fetchall()]
    has_avg = "buy_avg" in cols

    if has_avg:
        buy_top = con.execute("""
            SELECT broker_name, net_lots, buy_avg, buy_lots, sell_lots, sell_avg
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_lots > 0
            ORDER BY net_lots DESC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
        sell_top = con.execute("""
            SELECT broker_name, net_lots, buy_avg, buy_lots, sell_lots, sell_avg
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_lots < 0
            ORDER BY net_lots ASC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
    else:
        buy_top = con.execute("""
            SELECT broker_name, 0, 0, 0, 0, 0
            FROM broker_rank WHERE date=? AND stock_code=? AND net_volume > 0
            ORDER BY net_volume DESC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
        sell_top = con.execute("""
            SELECT broker_name, 0, 0, 0, 0, 0
            FROM broker_rank WHERE date=? AND stock_code=? AND net_volume < 0
            ORDER BY net_volume ASC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()

    con.close()

    def _fmt(r):
        return {
            "name": r[0],
            "net_lots": abs(int(r[1])) if r[1] else 0,
            "buy_avg": float(r[2]) if r[2] else 0,
            "buy_lots": int(r[3]) if r[3] else 0,
            "sell_lots": int(r[4]) if r[4] else 0,
            "sell_avg": float(r[5]) if r[5] else 0,
        }

    return ([_fmt(r) for r in buy_top], [_fmt(r) for r in sell_top])


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "3036"
    data = fetch_broker_top15(code)
    print(f"\n{code} 券商分點 ({data['data_date']})")
    print(f"  合計買超: {data['total_buy_lots']:,}張  平均成本: ${data['avg_buy_cost']}")
    print(f"  合計賣超: {data['total_sell_lots']:,}張  平均成本: ${data['avg_sell_cost']}")
    print(f"\n  買超前15:")
    for b in data["buy_top"]:
        print(f"    {b['name']:15s} 買{b['buy_lots']:>6,} 賣{b['sell_lots']:>6,} 超{b['net_lots']:>+6,} ({b['pct']})")
    print(f"\n  賣超前15:")
    for b in data["sell_top"]:
        print(f"    {b['name']:15s} 買{b['buy_lots']:>6,} 賣{b['sell_lots']:>6,} 超{b['net_lots']:>6,} ({b['pct']})")
