#!/usr/bin/env python3
"""初始化 DuckDB 資料庫 schema。"""

import duckdb
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "market.duckdb")


def init_db(db_path: str = DB_PATH):
    con = duckdb.connect(db_path)

    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_price (
            date DATE,
            stock_code VARCHAR,
            stock_name VARCHAR,
            open_price DECIMAL(10,2),
            high_price DECIMAL(10,2),
            low_price DECIMAL(10,2),
            close_price DECIMAL(10,2),
            limit_up_price DECIMAL(10,2),
            limit_down_price DECIMAL(10,2),
            is_limit_up BOOLEAN,
            volume BIGINT,
            trade_value BIGINT,
            change_price DECIMAL(10,2),
            change_pct DECIMAL(8,4),
            market VARCHAR,
            PRIMARY KEY (date, stock_code)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS stock_meta (
            stock_code VARCHAR,
            stock_name VARCHAR,
            industry VARCHAR,
            sector_group VARCHAR,
            market VARCHAR,
            updated_at DATE,
            PRIMARY KEY (stock_code)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_theme (
            date DATE,
            theme_name VARCHAR,
            theme_summary TEXT,
            key_driver TEXT,
            stock_list VARCHAR[],
            stock_count INTEGER,
            PRIMARY KEY (date, theme_name)
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS disposal_stock (
            stock_code VARCHAR,
            stock_name VARCHAR,
            start_date DATE,
            end_date DATE,
            reason TEXT,
            status VARCHAR,
            PRIMARY KEY (stock_code, start_date)
        )
    """)

    con.close()
    print(f"✓ 資料庫初始化完成: {db_path}")


if __name__ == "__main__":
    init_db()
