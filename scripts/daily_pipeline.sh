#!/bin/bash
# 戰情儀表板每日本機 pipeline
# 排程：每交易日 21:00（等 market_daily + market_supplementary 都更新完）
# 流程：跑 pipeline → build site → git push → GitHub Pages 自動部署
set -euo pipefail

PROJECT_DIR="/Users/slking/戰情儀表板"
VENV="$PROJECT_DIR/.venv/bin/python3"
LOG_DIR="/Users/slking/Library/Logs/taiwan-stock-dashboard"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/dashboard-pipeline.log"

cd "$PROJECT_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') 戰情儀表板 Pipeline ===" >> "$LOG"

# Step 1: 跑 daily pipeline（爬蟲+分類+LLM+分點+處置）
echo "[1/4] Running pipeline..." >> "$LOG"
"$VENV" crawler/daily_run.py >> "$LOG" 2>&1

# Step 2: 嘗試補救近期 partial 日（TWSE/TPEx 上游延遲時自動補）
echo "[2/4] Backfill partial days..." >> "$LOG"
"$VENV" crawler/backfill_partial.py >> "$LOG" 2>&1 || true

# Step 3: Build 靜態網站
echo "[3/4] Building site..." >> "$LOG"
"$VENV" generator/build_site.py >> "$LOG" 2>&1

# Step 4: Git push
echo "[4/4] Pushing to GitHub..." >> "$LOG"
git add -A
git commit -m "daily update $(date +%Y%m%d)" >> "$LOG" 2>&1 || true
git push >> "$LOG" 2>&1

echo "=== Done ===" >> "$LOG"
