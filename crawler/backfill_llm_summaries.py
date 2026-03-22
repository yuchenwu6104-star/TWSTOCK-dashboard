#!/usr/bin/env python3
"""批次補歷史 LLM 族群摘要 + 個股原因。"""

import duckdb
import datetime
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.init_db import DB_PATH
from crawler.llm_summary import call_llm, _try_parse_json


def backfill_all():
    con = duckdb.connect(DB_PATH)

    # 找所有空摘要的族群
    rows = con.execute("""
        SELECT date, theme_name, stock_list, stock_count
        FROM daily_theme
        WHERE length(theme_summary) < 5
        ORDER BY date, stock_count DESC
    """).fetchall()
    print(f"需要補: {len(rows)} 個族群摘要")

    # 讀所有股票名稱
    names = {}
    name_rows = con.execute("SELECT stock_code, stock_name FROM stock_meta").fetchall()
    for r in name_rows:
        names[r[0]] = r[1]

    # 也讀 daily_price 的股名（stock_meta 可能沒有的）
    price_names = con.execute("SELECT DISTINCT stock_code, stock_name FROM daily_price").fetchall()
    for r in price_names:
        if r[0] not in names:
            names[r[0]] = r[1]

    # 確保 stock_reason 表存在
    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_reason (
            date DATE, stock_code VARCHAR, reason TEXT,
            PRIMARY KEY (date, stock_code)
        )
    """)

    success = 0
    fail = 0
    _junk = ("80字", "30字", "20字", "<填入", "<此股", "<族群", "<關鍵", "填入>")

    for d, theme_name, stock_codes, stock_count in rows:
        if not stock_codes:
            continue

        # 組 stock info
        stock_info = []
        for code in stock_codes[:15]:
            name = names.get(code, code)
            # 讀收盤價
            pr = con.execute("SELECT close_price FROM daily_price WHERE date=? AND stock_code=?",
                             [d, code]).fetchone()
            price = float(pr[0]) if pr and pr[0] else 0
            stock_info.append(f"- {name}({code}) ${price}")

        if not stock_info:
            continue

        reasons_template = ", ".join(
            '"' + c + '": "<此股漲停原因>"' for c in stock_codes[:15]
        )
        prompt = (
            f"你是台股分析師。{d}「{theme_name}」族群 {stock_count} 支漲停：\n"
            f"{chr(10).join(stock_info)}\n\n"
            f"分析漲停原因，只回 JSON，不要 markdown：\n"
            f'{{"summary": "<族群整體漲停原因>", '
            f'"driver": "<關鍵驅動因子>", '
            f'"reasons": {{{reasons_template}}}}}'
        )

        text = call_llm(prompt)
        parsed = _try_parse_json(text) if text else None

        if parsed and isinstance(parsed, dict):
            summary = parsed.get("summary", "")
            driver = parsed.get("driver", "")

            if any(j in summary for j in _junk):
                summary = f"{theme_name}族群今日有{stock_count}檔漲停"
                driver = ""

            con.execute("UPDATE daily_theme SET theme_summary=?, key_driver=? WHERE date=? AND theme_name=?",
                        [summary, driver, d, theme_name])

            reasons = parsed.get("reasons", {})
            if not isinstance(reasons, dict):
                reasons = {}
            for code, reason in reasons.items():
                if reason and not any(j in reason for j in _junk):
                    con.execute("INSERT OR REPLACE INTO stock_reason VALUES (?,?,?)",
                                [d, code, reason])

            success += 1
            print(f"  ✓ {d} {theme_name} ({stock_count}股)")
        else:
            # fallback
            con.execute("UPDATE daily_theme SET theme_summary=? WHERE date=? AND theme_name=?",
                        [f"{theme_name}族群今日有{stock_count}檔漲停", d, theme_name])
            fail += 1
            print(f"  ✗ {d} {theme_name}")

        time.sleep(0.5)

    con.close()
    print(f"\n✅ 完成: {success} 成功, {fail} 失敗")


if __name__ == "__main__":
    backfill_all()
