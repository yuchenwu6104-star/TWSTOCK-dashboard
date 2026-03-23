"""Centralized database paths and shared utilities."""

import os

# 本機外部 DB（由 launchd 排程維護）
_BASE = "/Users/slking/taiwan_stock_dashboard"
DISP_DB = os.path.join(_BASE, "處置股研究", "disposition_research.duckdb")
MARKET_DB = os.path.join(_BASE, "收盤觀察", "market_daily.duckdb")
SUPP_DB = os.path.join(_BASE, "收盤觀察", "market_supplementary.duckdb")
UNIVERSE_DB = os.path.join(_BASE, "backend", "heatmap_history.db")
BACKFILL_SCRIPT = os.path.join(_BASE, "處置股研究", "official_event_backfill.py")

# 本地工作 DB
DB_PATH = os.path.join(os.path.dirname(__file__), "market.duckdb")
