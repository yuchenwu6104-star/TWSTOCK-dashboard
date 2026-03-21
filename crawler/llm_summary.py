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
        "messages": [
            {"role": "system", "content": "你是台股分析師。只回傳 JSON，不要 markdown 或其他文字。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(cfg["url"], data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=60)
    data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    # MiniMax M2.5/M2.7 帶 <think> CoT
    if "<think>" in text:
        idx = text.rfind("</think>")
        if idx >= 0:
            after = text[idx + 8:].strip()
            if after:
                return after
        # </think> 後面是空的 → JSON 藏在 <think> 裡面，直接從全文提取
        # 移除 <think> 和 </think> 標籤
        text = re.sub(r"</?think>", "", text)
    text = text.strip()
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

        stock_codes_str = ", ".join(f'"{s["stock_code"]}"' for s in stocks[:15])

        prompt = (
            f"你是台股分析師。{trade_date}「{theme_name}」族群 {len(stocks)} 支漲停：\n"
            f"{stock_info}\n\n"
            f"用繁體中文分析，只回 JSON（不要 markdown 或解釋）：\n"
            f'{{"summary": "該族群漲停的驅動因子，80字內",'
            f'"driver": "一句話關鍵驅動，20字內",'
            f'"reasons": {{{stock_codes_str}各寫30字內該股漲停原因}}}}'
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
    """嘗試從 LLM 回傳中提取 JSON。嘗試所有 {...} 區塊，找含 summary 的那個。"""
    if not text:
        return None
    # 移除 markdown code fences
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()
    # 先直接 parse
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            return d
    except (json.JSONDecodeError, ValueError):
        pass

    # 找所有頂層 {...} blocks，取含 "summary" 的最長那個
    blocks = []
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                blocks.append(text[start:i + 1])
                start = None

    # 優先找含 "summary" 的
    for block in reversed(blocks):
        if "summary" in block:
            try:
                d = json.loads(block)
                if isinstance(d, dict):
                    return d
            except (json.JSONDecodeError, ValueError):
                continue

    # 退而求其次，找任何能 parse 的
    for block in reversed(blocks):
        try:
            d = json.loads(block)
            if isinstance(d, dict):
                return d
        except (json.JSONDecodeError, ValueError):
            continue

    return None
