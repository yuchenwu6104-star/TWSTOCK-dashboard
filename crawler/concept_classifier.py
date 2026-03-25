#!/usr/bin/env python3
"""概念分類模組 v3：基於 universe.sector_group 精確分類 + 主題歸組。

分類流程：
1. 從 universe 表取每支股票的 sector_group（由 sector_mapping.py 維護，100% 覆蓋）
2. 用 THEME_MAP 把 79 個 sector_group 歸成 ~15 個顯示主題
3. 少於 2 支的主題合進「個股表現亮點」
"""

import duckdb
import os

from db.paths import UNIVERSE_DB

# ============================================================
# sector_group → 顯示主題 mapping
# key = universe.sector_group, value = 顯示主題名
# ============================================================
THEME_MAP = {
    # --- 半導體 ---
    "晶圓代工":       "半導體",
    "IC設計":        "IC設計／AI邊緣運算",
    "功率半導體":      "IC設計／AI邊緣運算",
    "驅動IC":        "IC設計／AI邊緣運算",
    "矽智財":        "IC設計／AI邊緣運算",
    "CIS/感測":      "IC設計／AI邊緣運算",
    "封測":          "半導體檢測／封測",
    "測試/量測":      "半導體檢測／封測",
    "半導體設備":      "半導體設備／材料",
    "DRAM":          "DRAM／記憶體",
    "記憶體模組":      "DRAM／記憶體",
    "Flash/儲存IC":  "DRAM／記憶體",
    "半導體-其他":     "半導體",

    # --- 電子零組件 ---
    "PCB":           "PCB／CCL",
    "CCL/銅箔基板":   "PCB／CCL",
    "被動元件":       "被動元件",
    "連接器":         "連接器／線材",
    "線材/連接":      "連接器／線材",
    "電源供應器":      "AI伺服器／零組件",
    "電源/電池":      "AI伺服器／零組件",
    "散熱":          "AI伺服器／零組件",
    "散熱模組":       "AI伺服器／零組件",
    "機殼/機構":      "AI伺服器／零組件",
    "電子零組件":      "電子零組件",

    # --- 光電 ---
    "面板":          "面板／顯示",
    "顯示技術":       "面板／顯示",
    "LED":           "LED／光電",
    "光學鏡頭":       "LED／光電",
    "光電":          "LED／光電",
    "太陽能":        "太陽能／綠能",

    # --- 電腦 ---
    "伺服器/雲端":    "AI伺服器／零組件",
    "NB/PC品牌":     "電腦及週邊",
    "IPC工業電腦":    "電腦及週邊",
    "電腦及週邊":     "電腦及週邊",

    # --- 通信 ---
    "網通設備":       "網通",
    "光通訊":        "光通訊／矽光子",
    "電信服務":       "網通",
    "衛星/天線":      "網通",
    "通信網路":       "網通",

    # --- 其他電子 ---
    "EMS/代工":      "電子代工",
    "ODM/代工":      "電子代工",
    "其他電子":       "電子代工",

    # --- 生技 ---
    "新藥研發":       "生技醫材",
    "醫材":          "生技醫材",
    "生技":          "生技醫材",
    "生技醫療":       "生技醫材",
    "製藥":          "生技醫材",

    # --- 金融 ---
    "金融保險":       "金融保險",
    "租賃":          "金融保險",

    # --- 電機 ---
    "電機機械":       "電機機械",
    "設備/自動化":    "電機機械",

    # --- 傳產 ---
    "鋼鐵":          "鋼鐵",
    "塑膠":          "塑化／化工",
    "化學":          "塑化／化工",
    "橡膠":          "塑化／化工",
    "油電燃氣":       "油電燃氣",
    "綠能環保":       "太陽能／綠能",
    "建材營造":       "營建／資產",
    "水泥":          "營建／資產",
    "玻璃陶瓷":       "營建／資產",
    "航運":          "航運",
    "食品":          "食品",
    "紡織纖維":       "紡織纖維",
    "觀光餐旅":       "觀光餐旅",
    "汽車":          "車用電子",

    # --- 其他 ---
    "資訊服務":       "資訊服務",
    "電子通路":       "電子通路",
    "通路/服務":      "電子通路",
    "電器電纜":       "電機機械",
    "數位雲端":       "資訊服務",
    "貿易百貨":       "其他",
    "居家生活":       "其他",
    "運動休閒":       "其他",
    "文化創意":       "其他",
    "農業科技":       "其他",
    "造紙":          "其他",
    "保全":          "其他",
    "其他":          "其他",
    "ETF":          "ETF",
}

# 主題 → icon
THEME_ICON = {
    "IC設計／AI邊緣運算": "🧠",
    "半導體":           "🏭",
    "半導體檢測／封測":    "🔍",
    "半導體設備／材料":    "⚙️",
    "DRAM／記憶體":      "💾",
    "AI伺服器／零組件":   "🖥️",
    "PCB／CCL":        "📋",
    "被動元件":          "🔩",
    "連接器／線材":       "🔌",
    "電子零組件":        "🔧",
    "面板／顯示":        "📺",
    "LED／光電":        "💡",
    "光通訊／矽光子":     "🔬",
    "網通":            "🌐",
    "電腦及週邊":        "💻",
    "電子代工":          "🏭",
    "太陽能／綠能":       "☀️",
    "生技醫材":          "💊",
    "鋼鐵":            "🔩",
    "塑化／化工":        "🛢️",
    "油電燃氣":          "⛽",
    "營建／資產":        "🏗️",
    "航運":            "🚢",
    "食品":            "🍚",
    "紡織纖維":          "🧵",
    "觀光餐旅":          "🏨",
    "車用電子":          "🚗",
    "金融保險":          "🏦",
    "資訊服務":          "💼",
    "電子通路":          "📦",
    "電機機械":          "⚙️",
    "貿易百貨":          "🏬",
    "其他":            "📊",
    "個股表現亮點":       "💼",
    "ETF":            "📈",
}


# ============================================================
# Ticker override — sector_mapping.py 分錯的，在這裡修正
# key = stock_code, value = 直接指定的顯示主題
# ============================================================
TICKER_OVERRIDE = {
    # 半導體測試介面（被 sector_mapping 歸到電子零組件-其他）
    "6217": "半導體檢測／封測",   # 中探針（探針卡）
    "7769": "半導體檢測／封測",   # 鴻勁精密（測試設備）
    "6510": "半導體檢測／封測",   # 精測（測試介面）
    "6515": "半導體設備／材料",   # 穎崴（探針卡）
    "4577": "半導體設備／材料",   # 達航科技（半導體材料）
    "6438": "半導體設備／材料",   # 迅得（自動化設備）
    # 光通訊（分到通信網路-其他）
    "6451": "光通訊／矽光子",    # 訊芯-KY（光收發模組）
    "4903": "光通訊／矽光子",    # 聯光通
    "5765": "光通訊／矽光子",    # 光星-KY
    # AI 伺服器（被歸到電腦週邊-其他）
    "3017": "AI伺服器／零組件",  # 奇鋐（散熱）
    "3653": "AI伺服器／零組件",  # 健策（散熱基板）
    "2059": "AI伺服器／零組件",  # 川湖（伺服器滑軌）
    "6669": "AI伺服器／零組件",  # 緯穎
    "8210": "AI伺服器／零組件",  # 勤誠（機殼）
    # 化合物半導體（被歸到晶圓代工）
    "3105": "半導體",           # 穩懋（GaAs 代工，保留半導體）
    # 記憶體相關
    "3006": "DRAM／記憶體",     # 晶豪科（記憶體 IC）
    "5351": "DRAM／記憶體",     # 鈺創（記憶體介面 IC）
    # PCB/CCL
    "6274": "PCB／CCL",        # 台燿（CCL）
    "6213": "PCB／CCL",        # 聯茂（CCL）
    "2383": "PCB／CCL",        # 台光電（CCL）
}


def load_universe_map() -> dict:
    """從 universe 表載入 stock_code → {name, industry, sector_group}。"""
    if not os.path.exists(UNIVERSE_DB):
        print(f"⚠ universe DB 不存在: {UNIVERSE_DB}")
        return {}
    con = duckdb.connect(UNIVERSE_DB, read_only=True)
    rows = con.execute("SELECT symbol, name, industry, sector_group FROM universe").fetchall()
    con.close()
    return {
        r[0]: {"name": r[1], "industry": r[2] or "", "sector_group": r[3] or ""}
        for r in rows
    }


def classify_stocks(limit_up_stocks: list[dict], universe_map: dict = None) -> dict:
    """將漲停股分群。

    流程：
    1. 查 universe 的 sector_group
    2. 用 THEME_MAP 歸到顯示主題
    3. 少於 2 支合進「個股表現亮點」
    """
    if universe_map is None:
        universe_map = load_universe_map()

    themes = {}

    for stock in limit_up_stocks:
        code = stock["stock_code"]
        meta = universe_map.get(code, {})
        sector = meta.get("sector_group", "")
        industry = meta.get("industry", "")

        # 優先用 ticker override
        theme = TICKER_OVERRIDE.get(code)
        # 否則用 THEME_MAP
        if not theme:
            theme = THEME_MAP.get(sector)
        if not theme:
            theme = THEME_MAP.get(industry, "其他")

        # 跳過 ETF
        if theme == "ETF":
            continue

        icon = THEME_ICON.get(theme, "📊")
        if theme not in themes:
            themes[theme] = {"stocks": [], "icon": icon}
        themes[theme]["stocks"].append(stock)

    # 少於 2 支合進「個股表現亮點」
    final = {}
    singles = {"stocks": [], "icon": "💼"}
    for name, info in themes.items():
        if len(info["stocks"]) >= 2:
            final[name] = info
        else:
            singles["stocks"].extend(info["stocks"])

    if singles["stocks"]:
        final["個股表現亮點"] = singles

    # 按族群股數降序排
    return dict(sorted(final.items(), key=lambda x: len(x[1]["stocks"]), reverse=True))


def save_stock_meta(universe_map: dict, db_path: str):
    """將 universe 分類同步寫入本地 stock_meta 表。"""
    import datetime
    con = duckdb.connect(db_path)
    con.execute("DELETE FROM stock_meta")
    today = datetime.date.today()
    for code, meta in universe_map.items():
        con.execute("INSERT OR REPLACE INTO stock_meta VALUES (?, ?, ?, ?, ?, ?)",
                    [code, meta["name"], meta["industry"], meta["sector_group"], "", today])
    con.close()
    print(f"✓ stock_meta 同步: {len(universe_map)} 筆")
