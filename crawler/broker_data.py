#!/usr/bin/env python3
"""券商分點買賣超爬蟲（BSR bsContent.aspx + 2captcha）。"""

import base64
import csv
import io
import json
import os
import re
import time
import datetime
import duckdb
import requests

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH

BSR_BASE = "https://bsr.twse.com.tw/bshtm"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _load_2captcha_key() -> str:
    key = os.environ.get("TWOCAPTCHA_API_KEY", "")
    if key:
        return key
    env_path = "/Users/slking/.openclaw/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.strip().startswith("TWOCAPTCHA_API_KEY="):
                    return line.strip().split("=", 1)[1]
    return ""


CAPTCHA_KEY = _load_2captcha_key()


def _solve_bsr_captcha(session: requests.Session) -> tuple[str, str, str, str]:
    """取得 BSR 驗證碼並解碼。回傳 (captcha_text, viewstate, viewstate_gen, event_validation)。"""
    # 1. GET bsMenu.aspx
    r = session.get(f"{BSR_BASE}/bsMenu.aspx", headers=HEADERS, timeout=15)
    text = r.text

    vs = re.search(r'__VIEWSTATE.*?value="([^"]+)"', text).group(1)
    vsg = re.search(r'__VIEWSTATEGENERATOR.*?value="([^"]+)"', text).group(1)
    ev = re.search(r'__EVENTVALIDATION.*?value="([^"]+)"', text).group(1)
    guid = re.search(r'CaptchaImage\.aspx\?guid=([a-f0-9-]+)', text).group(1)

    # 2. GET captcha image
    r2 = session.get(f"{BSR_BASE}/CaptchaImage.aspx?guid={guid}", headers=HEADERS, timeout=15)
    img_b64 = base64.b64encode(r2.content).decode()

    # 3. 送 2captcha（用 text mode 避免 JSON parse 問題）
    sub = requests.post("https://2captcha.com/in.php", data={
        "key": CAPTCHA_KEY, "method": "base64", "body": img_b64,
    }, timeout=30)
    if "OK|" not in sub.text:
        raise RuntimeError(f"2captcha submit failed: {sub.text}")
    task_id = sub.text.split("|")[1].strip()

    # 4. 輪詢
    for _ in range(20):
        time.sleep(3)
        res = requests.get("https://2captcha.com/res.php", params={
            "key": CAPTCHA_KEY, "action": "get", "id": task_id,
        }, timeout=15)
        if "OK|" in res.text:
            return res.text.split("|")[1].strip(), vs, vsg, ev
        if "CAPCHA_NOT_READY" in res.text:
            continue
        raise RuntimeError(f"2captcha error: {res.text}")

    raise RuntimeError("2captcha timeout")


def fetch_broker_for_stock(stock_code: str, session: requests.Session = None,
                           captcha_text: str = None, vs: str = None,
                           vsg: str = None, ev: str = None) -> list[dict]:
    """查詢單支股票的券商分點。如果沒給 session/captcha 會自己解。"""
    if session is None:
        session = requests.Session()
    if captcha_text is None:
        captcha_text, vs, vsg, ev = _solve_bsr_captcha(session)

    # POST 查詢
    data = {
        "__EVENTTARGET": "", "__EVENTARGUMENT": "", "__LASTFOCUS": "",
        "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
        "RadioButton_Normal": "RadioButton_Normal",
        "TextBox_Stkno": stock_code,
        "CaptchaControl1": captcha_text,
        "btnOK": "查詢",
    }
    r = session.post(f"{BSR_BASE}/bsMenu.aspx", data=data, headers=HEADERS, timeout=30)

    # 拿結果
    r2 = session.get(f"{BSR_BASE}/bsContent.aspx", headers=HEADERS, timeout=15)
    if r2.status_code != 200 or stock_code not in r2.text:
        return []

    return _parse_bsr_content(r2.text, stock_code)


def _parse_bsr_content(html: str, stock_code: str) -> list[dict]:
    """解析 bsContent.aspx 的 CSV-like 內容。

    格式：每行左右各一筆（用 ,, 分隔），同券商多個價位需加總。
    序號,券商,價格,買進股數,賣出股數,,序號,券商,價格,買進股數,賣出股數
    回傳含加權均價。
    """
    # broker -> {buy_shares, sell_shares, buy_cost, sell_cost}
    broker_agg = {}

    def _process_record(parts: list[str]):
        if len(parts) < 5:
            return
        broker = parts[1].strip()
        if not broker:
            return
        try:
            price = float(parts[2].strip().replace(",", "") or 0)
            buy = int(parts[3].strip().replace(",", "") or 0)
            sell = int(parts[4].strip().replace(",", "") or 0)
        except ValueError:
            return
        if broker not in broker_agg:
            broker_agg[broker] = {"buy_shares": 0, "sell_shares": 0,
                                  "buy_cost": 0.0, "sell_cost": 0.0}
        broker_agg[broker]["buy_shares"] += buy
        broker_agg[broker]["sell_shares"] += sell
        broker_agg[broker]["buy_cost"] += price * buy
        broker_agg[broker]["sell_cost"] += price * sell

    lines = html.split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("券商") or line.startswith("股票") or line.startswith("序號"):
            continue
        halves = line.split(",,")
        for half in halves:
            parts = [p.strip().replace('"', '').replace('=', '') for p in half.split(",")]
            if len(parts) >= 5:
                _process_record(parts)

    result = []
    for broker, d in broker_agg.items():
        bid = broker[:4] if len(broker) >= 4 else broker
        bname = re.sub(r'\s+', '', broker[4:]) if len(broker) > 4 else broker
        buy_s = d["buy_shares"]
        sell_s = d["sell_shares"]
        net = buy_s - sell_s
        buy_avg = round(d["buy_cost"] / buy_s, 2) if buy_s > 0 else 0
        sell_avg = round(d["sell_cost"] / sell_s, 2) if sell_s > 0 else 0
        # 轉張數（股 / 1000）
        buy_lots = buy_s // 1000
        sell_lots = sell_s // 1000
        net_lots = net // 1000
        if buy_s > 0 or sell_s > 0:
            result.append({
                "broker_id": bid,
                "broker_name": bname,
                "buy_volume": buy_s, "sell_volume": sell_s, "net_volume": net,
                "buy_lots": buy_lots, "sell_lots": sell_lots, "net_lots": net_lots,
                "buy_avg": buy_avg, "sell_avg": sell_avg,
            })

    result.sort(key=lambda x: abs(x["net_volume"]), reverse=True)
    return result


def fetch_multiple_stocks(stock_codes: list[str], max_retries: int = 3) -> dict:
    """批次查詢多支股票。每支需要一次驗證碼。回傳 {code: [broker_list]}。"""
    results = {}

    for code in stock_codes:
        for attempt in range(max_retries):
            try:
                session = requests.Session()
                captcha, vs, vsg, ev = _solve_bsr_captcha(session)
                print(f"  [broker] {code} captcha={captcha}")
                brokers = fetch_broker_for_stock(code, session, captcha, vs, vsg, ev)
                if brokers:
                    results[code] = brokers
                    print(f"  [broker] {code}: {len(brokers)} 券商")
                else:
                    print(f"  [broker] {code}: 無資料（驗證碼可能錯）")
                time.sleep(1)
                break
            except Exception as e:
                print(f"  [broker] {code} attempt {attempt+1} failed: {e}")
                time.sleep(2)

    return results


def save_broker_data(all_brokers: dict, trade_date: datetime.date, db_path: str = DB_PATH):
    """寫入 broker_rank 表（含均價與張數）。"""
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
    for stock_code, brokers in all_brokers.items():
        for b in brokers:
            con.execute("INSERT INTO broker_rank VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        [trade_date, stock_code, b["broker_id"], b["broker_name"],
                         b["buy_volume"], b["sell_volume"], b["net_volume"],
                         b.get("buy_lots", 0), b.get("sell_lots", 0), b.get("net_lots", 0),
                         b.get("buy_avg", 0), b.get("sell_avg", 0)])
            count += 1
    con.close()
    print(f"✓ broker_rank: {count} 筆")


def get_top_brokers(stock_code: str, trade_date: datetime.date,
                    top_n: int = 15, db_path: str = DB_PATH) -> tuple[list, list]:
    """撈某支股票的買超/賣超前 N 名（含均價張數）。"""
    con = duckdb.connect(db_path, read_only=True)

    # 檢查表是否有新欄位
    cols = [r[0] for r in con.execute("DESCRIBE broker_rank").fetchall()]
    has_avg = "buy_avg" in cols

    if has_avg:
        buy_top = con.execute("""
            SELECT broker_name, net_lots, buy_avg, buy_lots, sell_lots, sell_avg,
                   buy_volume, sell_volume, net_volume
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_volume > 0
            ORDER BY net_volume DESC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
        sell_top = con.execute("""
            SELECT broker_name, net_lots, buy_avg, buy_lots, sell_lots, sell_avg,
                   buy_volume, sell_volume, net_volume
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_volume < 0
            ORDER BY net_volume ASC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
    else:
        buy_top = con.execute("""
            SELECT broker_name, 0, 0, 0, 0, 0, buy_volume, sell_volume, net_volume
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_volume > 0
            ORDER BY net_volume DESC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()
        sell_top = con.execute("""
            SELECT broker_name, 0, 0, 0, 0, 0, buy_volume, sell_volume, net_volume
            FROM broker_rank
            WHERE date = ? AND stock_code = ? AND net_volume < 0
            ORDER BY net_volume ASC LIMIT ?
        """, [trade_date, stock_code, top_n]).fetchall()

    con.close()

    def _fmt(r, is_sell=False):
        return {
            "name": r[0],
            "net_lots": abs(r[1]) if r[1] else abs(r[8]) // 1000,
            "buy_avg": float(r[2]) if r[2] else 0,
            "buy_lots": r[3] if r[3] else r[6] // 1000,
            "sell_lots": r[4] if r[4] else r[7] // 1000,
            "sell_avg": float(r[5]) if r[5] else 0,
            "buy": r[6], "sell": r[7], "net": r[8],
        }

    return (
        [_fmt(r) for r in buy_top],
        [_fmt(r, is_sell=True) for r in sell_top],
    )


if __name__ == "__main__":
    # 測試：查 2330
    brokers = fetch_broker_for_stock("2330")
    print(f"\n2330 台積電 券商分點 ({len(brokers)} 家):")
    for b in brokers[:10]:
        print(f"  {b['broker_id']}{b['broker_name']:10s}  買{b['buy_volume']:>8,}  賣{b['sell_volume']:>8,}  淨{b['net_volume']:>+8,}")
