#!/usr/bin/env python3
"""LLM 族群摘要 + 個股漲停原因生成：Claude API → MiniMax fallback。"""

import json
import os
import urllib.request
import urllib.error
import re


def _load_env_keys() -> dict:
    keys = {}
    env_path = "/Users/slking/.openclaw/.env"
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    keys[k.strip()] = v.strip()
    for k in ("ANTHROPIC_API_KEY", "MINIMAX_API_KEY"):
        env_val = os.environ.get(k)
        if env_val:
            keys[k] = env_val
    return keys


_KEYS = _load_env_keys()

LLM_CHAIN = [
    {
        "name": "Claude",
        "url": "https://api.anthropic.com/v1/messages",
        "api_key": _KEYS.get("ANTHROPIC_API_KEY", ""),
        "model": "claude-haiku-4-5-20251001",
        "type": "anthropic",
    },
    {
        "name": "MiniMax-M2.5",
        "url": "https://api.minimax.io/v1/chat/completions",
        "api_key": _KEYS.get("MINIMAX_API_KEY", ""),
        "model": "MiniMax-M2.5",
        "type": "openai",
    },
    {
        "name": "MiniMax-M2.7",
        "url": "https://api.minimax.io/v1/chat/completions",
        "api_key": _KEYS.get("MINIMAX_API_KEY", ""),
        "model": "MiniMax-M2.7",
        "type": "openai",
    },
]


def _call_anthropic(cfg: dict, prompt: str) -> str | None:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    return data["content"][0]["text"]


def _call_openai_compat(cfg: dict, prompt: str) -> str | None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text)
    return text


def call_llm(prompt: str) -> str | None:
    for cfg in LLM_CHAIN:
        if not cfg["api_key"]:
            continue
        try:
            if cfg["type"] == "anthropic":
                text = _call_anthropic(cfg, prompt)
            else:
                text = _call_openai_compat(cfg, prompt)
            print(f"  [LLM] {cfg['name']} ✓")
            return text
        except urllib.error.HTTPError as e:
            print(f"  [LLM] {cfg['name']} HTTP {e.code}，下一個")
        except Exception as e:
            print(f"  [LLM] {cfg['name']} 失敗: {e}，下一個")
    print("  [LLM] 所有 API 皆失敗")
    return None


def generate_theme_summaries(themes: dict, trade_date: str) -> dict:
    """為每個族群生成摘要 + 每支股票個別漲停原因。

    回傳 {theme_name: {summary, driver, stock_reasons: {code: reason}}}
    """
    results = {}

    for theme_name, stocks in themes.items():
        stock_info = "\n".join(
            f"- {s.get('stock_name', '')}({s['stock_code']}) ${s.get('close_price', 0)}"
            for s in stocks[:15]
        )

        # 構建 reasons 模板
        reason_keys = ", ".join(f'"{s["stock_code"]}": "原因"' for s in stocks[:15])

        prompt = (
            f"台股分析師任務。{trade_date}「{theme_name}」族群 {len(stocks)} 支漲停：\n"
            f"{stock_info}\n\n"
            f"直接回傳 JSON，不要任何其他文字：\n"
            f'{{"summary":"族群漲停原因80字","driver":"關鍵驅動20字","reasons":{{{reason_keys}}}}}'
        )

        text = call_llm(prompt)
        parsed = _try_parse_json(text) if text else None

        if parsed and isinstance(parsed, dict):
            reasons = parsed.get("reasons", {})
            results[theme_name] = {
                "summary": parsed.get("summary", ""),
                "driver": parsed.get("driver", ""),
                "stock_reasons": reasons,
            }
        else:
            # Fallback: 嘗試舊格式解析
            summary, driver = "", ""
            if text:
                for line in text.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("摘要"):
                        summary = line.split("：", 1)[-1].strip()
                    elif line.startswith("驅動因子"):
                        driver = line.split("：", 1)[-1].strip()
                if not summary:
                    summary = text.strip()[:200]
            results[theme_name] = {
                "summary": summary or f"{theme_name}族群今日有{len(stocks)}檔漲停",
                "driver": driver or "資料待更新",
                "stock_reasons": {},
            }

    return results


def _try_parse_json(text: str) -> dict | None:
    """嘗試從 LLM 回傳中提取 JSON。"""
    if not text:
        return None
    # 先直接 parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # 找最後一個 {...} block
    depth = 0
    start = None
    best = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                best = text[start:i + 1]
    if best:
        try:
            return json.loads(best)
        except (json.JSONDecodeError, ValueError):
            pass
    return None
