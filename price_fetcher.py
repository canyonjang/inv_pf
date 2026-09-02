"""
종가 수집 모듈 (외부 라이브러리 없음)
-------------------------------------
pykrx / yfinance 를 쓰지 않습니다. requests 만으로 직접 받아옵니다.
Streamlit Cloud 의 파이썬 버전이 올라가도 빌드가 깨지지 않습니다.

수집 소스
    국내 주식·ETF  : KRX 공개 JSON (전종목 일괄, 요청 2회)
                     → 빠진 종목만 네이버 금융으로 보충
    코스피지수     : KRX 지수 시세 → 실패 시 네이버
    해외 주식·ETF  : Stooq CSV
    S&P500 · 환율  : Stooq CSV → 환율은 실패 시 네이버
    가상자산       : 업비트 공개 API

교수 화면의 [기준가 업데이트] 버튼을 눌렀을 때만 호출됩니다.
"""

import json
import time
from datetime import date, timedelta

import requests

from universe import ALL_TICKERS

TIMEOUT = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ------------------------------------------------------------------
# 기준일
# ------------------------------------------------------------------
def previous_business_day(base: date | None = None) -> date:
    d = (base or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:          # 5=토, 6=일
        d -= timedelta(days=1)
    return d


def _num(x):
    """'70,000' → 70000.0"""
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return None


# ==================================================================
# 국내 — KRX 전종목 일괄
# ==================================================================
KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
}


def _krx_post(payload, log):
    try:
        r = requests.post(KRX_URL, data=payload, headers=KRX_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log(f"KRX 요청 실패: {exc}")
        return None


def _fetch_krx_bulk(target: date, log):
    """주식 전종목 + ETF 전종목 + 코스피지수를 요청 3회로 받는다."""
    ymd = target.strftime("%Y%m%d")
    out = {}

    jobs = [
        ({"bld": "dbms/MDC/STAT/standard/MDCSTAT01501", "mktId": "ALL",
          "trdDd": ymd, "share": "1", "money": "1", "csvxls_isNo": "false"}, "주식"),
        ({"bld": "dbms/MDC/STAT/standard/MDCSTAT04301", "mktId": "ALL",
          "trdDd": ymd, "share": "1", "money": "1", "csvxls_isNo": "false"}, "ETF"),
    ]
    for payload, tag in jobs:
        data = _krx_post(payload, log)
        if not data:
            continue
        rows = data.get("OutBlock_1") or data.get("output") or []
        if not rows:
            log(f"KRX {tag} 응답이 비어 있습니다 (휴장일일 수 있습니다)")
        for row in rows:
            code = (row.get("ISU_SRT_CD") or "").strip()
            close = _num(row.get("TDD_CLSPRC"))
            if code and close and close > 0:
                out[code] = close

    idx = _krx_post({"bld": "dbms/MDC/STAT/standard/MDCSTAT00101",
                     "idxIndMidclssCd": "01", "trdDd": ymd,
                     "share": "1", "money": "1", "csvxls_isNo": "false"}, log)
    if idx:
        for row in (idx.get("OutBlock_1") or []):
            if (row.get("IDX_NM") or "").strip() in ("코스피", "KOSPI"):
                v = _num(row.get("CLSPRC_IDX"))
                if v:
                    out["BENCH-KOSPI"] = v
                break

    return out


# ==================================================================
# 국내 — 네이버 금융 (KRX 실패분 보충)
# ==================================================================
def _fetch_naver_one(symbol: str, target: date, log):
    """네이버 일별 시세에서 기준일 이하 마지막 종가를 가져온다."""
    start = (target - timedelta(days=15)).strftime("%Y%m%d")
    end = target.strftime("%Y%m%d")
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={symbol}&requestType=1&startTime={start}"
           f"&endTime={end}&timeframe=day")
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        rows = json.loads(r.text.replace("'", '"').strip())
        for row in reversed(rows[1:]):          # 첫 줄은 헤더
            close = _num(row[4])
            if close and close > 0:
                return close
    except Exception as exc:
        log(f"네이버 {symbol} 실패: {exc}")
    return None


def _fetch_naver_many(symbols, target, log):
    out = {}
    for s in symbols:
        v = _fetch_naver_one(s, target, log)
        if v:
            out[s] = v
        time.sleep(0.05)
    return out


# ==================================================================
# 해외 · 환율 — ① 야후 차트 API  ② Stooq CSV  ③ 네이버
# ------------------------------------------------------------------
# Stooq 는 클라우드 IP에서 빈 응답을 주는 경우가 있어 단독으로 쓰지 않는다.
# ==================================================================
YAHOO_HOSTS = ["https://query1.finance.yahoo.com",
               "https://query2.finance.yahoo.com"]


def _yahoo_symbol(ticker: str) -> str:
    if ticker == "BENCH-SP500":
        return "^GSPC"
    if ticker == "FX-USDKRW":
        return "KRW=X"
    return ticker


def _fetch_yahoo_one(ticker: str, target: date, log):
    """야후 차트 API에서 기준일 이하 마지막 종가를 가져온다."""
    sym = _yahoo_symbol(ticker)
    p1 = int(time.mktime((target - timedelta(days=25)).timetuple()))
    p2 = int(time.mktime((target + timedelta(days=1)).timetuple()))
    for host in YAHOO_HOSTS:
        url = (f"{host}/v8/finance/chart/{requests.utils.quote(sym, safe='')}"
               f"?period1={p1}&period2={p2}&interval=1d")
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Accept": "application/json"},
                             timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                continue
            closes = (res[0].get("indicators", {})
                      .get("quote", [{}])[0].get("close") or [])
            for c in reversed(closes):
                if c:
                    return float(c)
        except Exception as exc:
            log(f"야후 {sym} 실패: {exc}")
    return None


def _stooq_symbol(ticker: str) -> str:
    if ticker == "BENCH-SP500":
        return "^spx"
    if ticker == "FX-USDKRW":
        return "usdkrw"
    return f"{ticker.lower()}.us"


def _fetch_stooq_one(sym: str, target: date, log):
    d1 = (target - timedelta(days=25)).strftime("%Y%m%d")
    d2 = target.strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.lower().startswith("no data"):
            return None
        lines = text.splitlines()
        header = [h.strip().lower() for h in lines[0].split(",")]
        if "close" not in header:
            return None
        ci = header.index("close")
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) > ci:
                v = _num(parts[ci])
                if v and v > 0:
                    return v
    except Exception as exc:
        log(f"Stooq {sym} 실패: {exc}")
    return None


def _fetch_overseas(tickers, target, log):
    """야후 → Stooq 순으로 시도한다."""
    out, failed = {}, []
    for t in tickers:
        v = _fetch_yahoo_one(t, target, log)
        if v is None:
            v = _fetch_stooq_one(_stooq_symbol(t), target, log)
        if v:
            out[t] = v
        else:
            failed.append(t)
        time.sleep(0.08)
    if failed:
        log(f"해외 {len(failed)}종목 수집 실패: {', '.join(failed)}")
    return out


def _fetch_naver_fx(target, log):
    """네이버 시장지표 환율 차트."""
    s = (target - timedelta(days=20)).strftime("%Y%m%d") + "0000"
    e = target.strftime("%Y%m%d") + "2359"
    url = ("https://api.stock.naver.com/chart/marketindex/area"
           f"?category=exchange&reutersCode=FX_USDKRW"
           f"&startDateTime={s}&endDateTime={e}&type=day")
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Accept": "application/json"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        infos = r.json().get("priceInfos") or []
        for row in reversed(infos):
            v = _num(row.get("closePrice"))
            if v and v > 0:
                return v
    except Exception as exc:
        log(f"네이버 환율 실패: {exc}")
    return None


def _fetch_fx(target, log):
    """원달러 환율. 야후 → 네이버 → Stooq 순."""
    v = _fetch_yahoo_one("FX-USDKRW", target, log)
    if v:
        return v
    v = _fetch_naver_fx(target, log)
    if v:
        return v
    return _fetch_stooq_one("usdkrw", target, log)


# ==================================================================
# 가상자산 — 업비트
# ==================================================================
def _fetch_upbit(markets, target: date, log):
    out = {}
    to_param = f"{target.isoformat()}T23:59:59+09:00"
    for market in markets:
        try:
            r = requests.get("https://api.upbit.com/v1/candles/days",
                             params={"market": market, "to": to_param, "count": 1},
                             headers={"Accept": "application/json", "User-Agent": UA},
                             timeout=TIMEOUT)
            r.raise_for_status()
            rows = r.json()
            if rows:
                out[market] = float(rows[0]["trade_price"])
        except Exception as exc:
            log(f"{market} 실패: {exc}")
        time.sleep(0.05)
    return out


# ==================================================================
# 통합
# ==================================================================
def fetch_all(target: date):
    """
    반환: (rows, fx, logs, missing, missing_tickers)
        rows            : inv_pf_prices 에 넣을 dict 리스트
        fx              : 원달러 환율 (실패 시 None)
        logs            : 경고 메시지
        missing         : 수집 실패 종목명 (사람이 읽는 용도)
        missing_tickers : 수집 실패 티커 (수동 입력 UI 용도)
    """
    logs = []
    log = logs.append

    kr = [t for t, _, c, _, _ in ALL_TICKERS if c in ("kr_stock", "kr_etf")]
    us = [t for t, _, c, _, _ in ALL_TICKERS if c == "us"]
    crypto = [t for t, _, c, _, _ in ALL_TICKERS if c == "crypto"]

    prices = {}

    # 1) 국내 일괄
    prices.update(_fetch_krx_bulk(target, log))

    # 2) 빠진 국내 종목만 네이버로 보충
    need = [t for t in kr if t not in prices]
    want_kospi = "BENCH-KOSPI" not in prices
    if need or want_kospi:
        log(f"KRX에서 {len(need)}종목이 비어 네이버로 보충합니다.")
        syms = need + (["KOSPI"] if want_kospi else [])
        for k, v in _fetch_naver_many(syms, target, log).items():
            prices["BENCH-KOSPI" if k == "KOSPI" else k] = v

    # 3) 해외 + S&P500
    prices.update(_fetch_overseas(us + ["BENCH-SP500"], target, log))

    # 4) 가상자산
    prices.update(_fetch_upbit(crypto, target, log))

    # 5) 환율
    fx = _fetch_fx(target, log)
    if not fx:
        log("환율을 가져오지 못했습니다. 해외 종목 평가가 비어 있을 수 있습니다.")

    rows, missing, missing_tickers = [], [], []
    for ticker, name, cls, _high, cur in ALL_TICKERS:
        p = prices.get(ticker)
        if p is None:
            missing.append(f"{name}({ticker})")
            missing_tickers.append(ticker)
            continue
        rows.append({
            "ticker": ticker,
            "price_date": target.isoformat(),
            "close_price": round(float(p), 4),
            "fx_usdkrw": round(float(fx), 2) if fx else None,
        })

    return rows, fx, logs, missing, missing_tickers
