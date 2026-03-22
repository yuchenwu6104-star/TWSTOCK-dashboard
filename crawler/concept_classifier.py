#!/usr/bin/env python3
"""概念分類模組：sector_group + AI 主題關鍵字疊加。"""

import duckdb
import os

# 既有 universe 資料庫（唯讀）
UNIVERSE_DB = "/Users/slking/taiwan_stock_dashboard/backend/heatmap_history.db"

# AI 概念關鍵字對照表（疊加在 sector_group 之上）+ icon
AI_CONCEPT_OVERLAY = {
    "AI伺服器／零組件":  {"kw": ["散熱", "機殼", "電源", "伺服器", "GB200", "GB300", "CoWoS", "雲端"], "icon": "🖥️"},
    "CCL／銅箔基板":     {"kw": ["銅箔", "基板", "CCL", "玻纖"], "icon": "📋"},
    "PCB":              {"kw": ["PCB", "電路板", "HDI", "ABF"], "icon": "📋"},
    "網通":             {"kw": ["網通", "交換器", "路由器", "400G", "800G"], "icon": "🌐"},
    "光通訊／矽光子":    {"kw": ["光纖", "光模組", "矽光子", "光通訊"], "icon": "🔬"},
    "車用電子":          {"kw": ["車用", "ADAS", "電動車"], "icon": "🚗"},
    "半導體設備／材料":   {"kw": ["設備", "機台", "CVD", "ALD"], "icon": "⚙️"},
    "半導體檢測／封測":   {"kw": ["封測", "封裝", "測試介面", "探針"], "icon": "🔍"},
    "被動元件":          {"kw": ["被動元件", "電容", "電阻", "電感"], "icon": "🔩"},
    "DRAM／記憶體":      {"kw": ["DRAM", "記憶體", "HBM", "DDR"], "icon": "💾"},
    "IC設計／AI邊緣運算": {"kw": ["IC設計", "ASIC", "SoC", "RISC-V"], "icon": "🧠"},
    "面板":             {"kw": ["面板", "顯示器", "OLED", "Micro LED"], "icon": "📺"},
    "生技醫材":          {"kw": ["新藥", "生技", "臨床", "醫材", "藥證"], "icon": "💊"},
    "太陽能／綠能":      {"kw": ["儲能", "電池", "太陽能", "風電", "綠能"], "icon": "☀️"},
    "鋼鐵／鋼價":       {"kw": ["鋼鐵", "鋼價", "鋼材"], "icon": "🔩"},
    "塑化／化工":        {"kw": ["塑化", "塑膠", "化工", "油價"], "icon": "🛢️"},
    "營建／資產":        {"kw": ["營建", "建設", "都更", "資產"], "icon": "🏗️"},
    "航運":             {"kw": ["航運", "貨櫃", "散裝"], "icon": "🚢"},
}

# sector_group / industry → icon fallback
SECTOR_ICON_MAP = {
    "晶圓代工": "🏭", "IC設計": "🧠", "封測": "🔍", "功率半導體": "⚡",
    "驅動IC": "🖥️", "矽智財": "🧬", "CIS/感測": "📷", "半導體設備": "⚙️",
    "PCB": "📋", "CCL/銅箔基板": "📋", "被動元件": "🔩", "連接器": "🔌",
    "電源供應器": "🔋", "散熱模組": "🌡️", "面板": "📺", "LED": "💡",
    "光學鏡頭": "📷", "太陽能": "☀️", "伺服器/雲端": "☁️", "NB/PC品牌": "💻",
    "IPC工業電腦": "🖥️", "網通設備": "🌐", "光通訊": "🔬", "電信服務": "📡",
    "製藥": "💊", "新藥研發": "🧪", "醫材": "🏥", "生技": "🧬",
    "金融保險": "🏦", "航運": "🚢", "食品": "🍚", "觀光餐旅": "🏨",
    "建材營造": "🏗️", "鋼鐵": "🔩", "電子通路": "📦", "紡織纖維": "🧵",
    "電機機械": "⚙️", "化學": "🧪", "塑膠": "🛢️", "水泥": "🏛️",
    "ETF": "📈", "其他": "📰",
}


def load_universe_map() -> dict:
    """從 universe 表載入 stock_code → {industry, sector_group, name}。"""
    if not os.path.exists(UNIVERSE_DB):
        print(f"⚠ universe DB 不存在: {UNIVERSE_DB}")
        return {}
    con = duckdb.connect(UNIVERSE_DB, read_only=True)
    rows = con.execute("""
        SELECT symbol, name, industry, sector_group
        FROM universe
    """).fetchall()
    con.close()
    return {
        r[0]: {"name": r[1], "industry": r[2] or "", "sector_group": r[3] or ""}
        for r in rows
    }


def classify_stocks(limit_up_stocks: list[dict], universe_map: dict = None) -> dict:
    """將漲停股分群，回傳 {theme_name: [stock_dict, ...]}。

    分類優先順序：
    1. AI 概念 overlay（用 sector_group / industry / stock_name 比對關鍵字）
    2. sector_group（若有）
    3. industry（fallback）
    """
    if universe_map is None:
        universe_map = load_universe_map()

    themes = {}

    for stock in limit_up_stocks:
        code = stock["stock_code"]
        name = stock.get("stock_name", "")
        meta = universe_map.get(code, {})
        sector = meta.get("sector_group", "")
        industry = meta.get("industry", "")

        # 用來比對的文字池
        match_text = f"{name} {sector} {industry}".lower()

        # 嘗試 AI overlay
        assigned = False
        for concept, cfg in AI_CONCEPT_OVERLAY.items():
            if any(kw.lower() in match_text for kw in cfg["kw"]):
                themes.setdefault(concept, {"stocks": [], "icon": cfg["icon"]})
                themes[concept]["stocks"].append(stock)
                assigned = True
                break

        if not assigned:
            # fallback: sector_group → industry
            group = sector if sector else industry if industry else "其他"
            icon = SECTOR_ICON_MAP.get(group, SECTOR_ICON_MAP.get(industry, "📊"))
            themes.setdefault(group, {"stocks": [], "icon": icon})
            themes[group]["stocks"].append(stock)

    # --- 合併碎片族群 ---
    # 細分類 → 母族群
    MERGE_MAP = {
        "半導體-其他": "IC設計／AI邊緣運算",
        "NB/PC品牌": "電腦及週邊",
        "IPC工業電腦": "電腦及週邊",
        "ODM/代工": "電腦及週邊",
        "EMS/代工": "電腦及週邊",
        "測試/量測": "半導體檢測／封測",
        "驅動IC": "IC設計／AI邊緣運算",
        "功率半導體": "IC設計／AI邊緣運算",
        "矽智財": "IC設計／AI邊緣運算",
        "CIS/感測": "IC設計／AI邊緣運算",
        "記憶體模組": "DRAM／記憶體",
        "Flash/儲存IC": "DRAM／記憶體",
        "電源供應器": "電子零組件",
        "散熱模組": "AI伺服器／零組件",
        "顯示技術": "面板",
        "光學鏡頭": "光電",
        "衛星/天線": "網通",
        "電信服務": "網通",
        "製藥": "生技醫材",
        "新藥研發": "生技醫材",
        "醫材": "生技醫材",
        "生技": "生技醫材",
        "通路/服務": "生技醫材",
        "租賃": "金融保險",
        "保全": "其他",
    }

    merged = {}
    for name, info in themes.items():
        target = MERGE_MAP.get(name, name)
        if target not in merged:
            # 用目標族群的 icon，如果目標已在 themes 裡就用它的
            if target in themes:
                merged[target] = {"stocks": list(themes[target]["stocks"]), "icon": themes[target]["icon"]}
            else:
                icon = SECTOR_ICON_MAP.get(target, info["icon"])
                merged[target] = {"stocks": [], "icon": icon}
        if name != target:
            merged[target]["stocks"].extend(info["stocks"])

    # 確保沒被合併的也進來
    for name, info in themes.items():
        if name not in merged and name not in MERGE_MAP:
            merged[name] = info

    # 少於 2 支的合進「個股表現亮點」
    final = {}
    singles = {"stocks": [], "icon": "💼"}
    for name, info in merged.items():
        if len(info["stocks"]) >= 2:
            final[name] = info
        else:
            singles["stocks"].extend(info["stocks"])

    if singles["stocks"]:
        final["個股表現亮點"] = singles

    # 按族群股數降序排
    sorted_themes = dict(sorted(final.items(), key=lambda x: len(x[1]["stocks"]), reverse=True))
    return sorted_themes


def save_stock_meta(universe_map: dict, db_path: str):
    """將 universe 分類同步寫入本地 stock_meta 表。"""
    import datetime
    con = duckdb.connect(db_path)
    con.execute("DELETE FROM stock_meta")
    today = datetime.date.today()
    for code, meta in universe_map.items():
        con.execute("""
            INSERT OR REPLACE INTO stock_meta VALUES (?, ?, ?, ?, ?, ?)
        """, [code, meta["name"], meta["industry"], meta["sector_group"], "", today])
    con.close()
    print(f"✓ stock_meta 同步: {len(universe_map)} 筆")
