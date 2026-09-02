"""
승인 종목 리스트 (유니버스)
---------------------------
학생들은 이 목록 안에서만 종목을 고를 수 있습니다.
목록을 바꾸면 앱의 종목 선택지와 기준가 수집 대상이 함께 바뀝니다.

필드
    ticker      : 국내=6자리 종목코드 / 해외=야후 티커 / 가상자산=업비트 마켓코드
    name        : 화면 표시명
    asset_class : kr_stock | kr_etf | us | crypto | bench
    high_risk   : True면 고위험자산(합계 20% 상한 대상)
    currency    : KRW | USD
"""

SECURITIES = [
    # ---------------- 국내 주식 ----------------
    ("005930", "삼성전자",            "kr_stock", False, "KRW"),
    ("000660", "SK하이닉스",          "kr_stock", False, "KRW"),
    ("373220", "LG에너지솔루션",      "kr_stock", False, "KRW"),
    ("207940", "삼성바이오로직스",    "kr_stock", False, "KRW"),
    ("005380", "현대차",              "kr_stock", False, "KRW"),
    ("000270", "기아",                "kr_stock", False, "KRW"),
    ("035420", "NAVER",               "kr_stock", False, "KRW"),
    ("035720", "카카오",              "kr_stock", False, "KRW"),
    ("051910", "LG화학",              "kr_stock", False, "KRW"),
    ("105560", "KB금융",              "kr_stock", False, "KRW"),
    ("055550", "신한지주",            "kr_stock", False, "KRW"),
    ("034730", "SK",                  "kr_stock", False, "KRW"),
    ("015760", "한국전력",            "kr_stock", False, "KRW"),
    ("017670", "SK텔레콤",            "kr_stock", False, "KRW"),
    ("068270", "셀트리온",            "kr_stock", False, "KRW"),

    # ---------------- 국내 ETF ----------------
    ("069500", "KODEX 200",                 "kr_etf", False, "KRW"),
    ("229200", "KODEX 코스닥150",           "kr_etf", False, "KRW"),
    ("360750", "TIGER 미국S&P500",          "kr_etf", False, "KRW"),
    ("133690", "TIGER 미국나스닥100",       "kr_etf", False, "KRW"),
    ("305720", "KODEX 2차전지산업",         "kr_etf", False, "KRW"),
    ("091160", "KODEX 반도체",              "kr_etf", False, "KRW"),
    ("148070", "KOSEF 국고채10년",          "kr_etf", False, "KRW"),
    ("136340", "TIGER 단기통안채",          "kr_etf", False, "KRW"),
    ("132030", "KODEX 골드선물(H)",         "kr_etf", False, "KRW"),
    ("453850", "TIGER 미국배당다우존스",    "kr_etf", False, "KRW"),
    ("441640", "TIGER 미국나스닥100커버드콜", "kr_etf", False, "KRW"),

    # ---------------- 고위험: 레버리지 / 인버스 ----------------
    ("122630", "KODEX 레버리지",            "kr_etf", True,  "KRW"),
    ("252670", "KODEX 200선물인버스2X",     "kr_etf", True,  "KRW"),
    ("233740", "KODEX 코스닥150레버리지",   "kr_etf", True,  "KRW"),
    ("251340", "KODEX 코스닥150선물인버스", "kr_etf", True,  "KRW"),

    # ---------------- 해외 ETF / 주식 ----------------
    ("SPY",  "SPDR S&P500 ETF",             "us", False, "USD"),
    ("QQQ",  "Invesco QQQ (나스닥100)",     "us", False, "USD"),
    ("SCHD", "Schwab 미국배당 ETF",         "us", False, "USD"),
    ("JEPI", "JPMorgan 커버드콜 ETF",       "us", False, "USD"),
    ("TLT",  "iShares 미국장기국채 ETF",    "us", False, "USD"),
    ("AAPL", "Apple",                       "us", False, "USD"),
    ("MSFT", "Microsoft",                   "us", False, "USD"),
    ("NVDA", "NVIDIA",                      "us", False, "USD"),
    ("TSLA", "Tesla",                       "us", False, "USD"),
    ("AMZN", "Amazon",                      "us", False, "USD"),
    ("TQQQ", "ProShares 나스닥100 3배",     "us", True,  "USD"),
    ("SQQQ", "ProShares 나스닥100 -3배",    "us", True,  "USD"),

    # ---------------- 가상자산 ----------------
    ("KRW-BTC", "비트코인",   "crypto", True, "KRW"),
    ("KRW-ETH", "이더리움",   "crypto", True, "KRW"),
    ("KRW-XRP", "리플",       "crypto", True, "KRW"),
    ("KRW-SOL", "솔라나",     "crypto", True, "KRW"),
]

# 벤치마크 (학생은 매수할 수 없고, 비교용으로만 수집)
BENCHMARKS = [
    ("BENCH-KOSPI", "코스피지수",  "bench", False, "KRW"),
    ("BENCH-SP500", "S&P500지수",  "bench", False, "USD"),
]

ALL_TICKERS = SECURITIES + BENCHMARKS

# ---- 조회용 딕셔너리 ----
SEC_MAP = {t: {"name": n, "asset_class": c, "high_risk": h, "currency": cur}
           for t, n, c, h, cur in ALL_TICKERS}

TRADABLE = [t for t, *_ in SECURITIES]


def label(ticker: str) -> str:
    """화면에 표시할 이름."""
    info = SEC_MAP.get(ticker)
    if not info:
        return ticker
    mark = " ⚠️" if info["high_risk"] else ""
    return f"{info['name']}{mark}"


def is_high_risk(ticker: str) -> bool:
    return bool(SEC_MAP.get(ticker, {}).get("high_risk"))


def group_of(ticker: str) -> str:
    """선택 UI에서 묶어 보여줄 그룹명."""
    info = SEC_MAP.get(ticker, {})
    cls = info.get("asset_class")
    if info.get("high_risk"):
        return "고위험 (레버리지·인버스·가상자산)"
    return {
        "kr_stock": "국내 주식",
        "kr_etf": "국내 ETF",
        "us": "해외 주식·ETF",
        "crypto": "가상자산",
    }.get(cls, "기타")
