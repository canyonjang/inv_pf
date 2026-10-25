"""
종가 수집 모듈 (외부 라이브러리 없음)
-------------------------------------
pykrx / yfinance 를 쓰지 않습니다. requests 만으로 직접 받아옵니다.
Streamlit Cloud 의 파이썬 버전이 올라가도 빌드가 깨지지 않습니다.

수집 소스
    국내 주식·ETF  : 네이버 금융 (종목별)
                     ※ KRX 전종목 일괄 코드도 들어 있으나 기본 비활성화
                        (USE_KRX_BULK 를 True 로 바꾸면 켜짐)
    코스피지수     : 네이버 금융
    해외 주식·ETF  : 야후 차트 API → Stooq CSV
    S&P500 · 환율  : 야후 → 네이버 → Stooq
    가상자산       : 업비트 공개 API

교수 화면의 [기준가 업데이트] 버튼을 눌렀을 때만 호출됩니다.

── 날짜 검증 (2026-10 추가) ────────────────────────────────────────
한국 장이 끝난 저녁에 당일 기준가를 부르면, 미국 장은 아직 열리지도
않은 상태입니다(미 종가는 한국시간 다음 날 새벽 확정). 예전 코드는
"요청한 날짜 이하의 마지막 종가"를 말없이 가져와 요청한 날짜 칸에
저장했기 때문에, 하루 전 종가가 조용히 섞여 들어갔습니다.

이제는 모든 소스에서 **그 값이 실제로 어느 날짜의 종가인지**를 함께
받아, 요청한 날짜와 다르면 logs 에 "⚠" 로 시작하는 경고를 남깁니다.
app.py 는 "⚠" 로 시작하는 줄을 노란 경고 박스로 띄웁니다.
"""

import json
import time
from datetime import date, datetime, timedelta, timezone

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


def _ts_date(ts):
    """유닉스 타임스탬프 → 거래일(UTC 기준). 미국 일봉은 장 시작 시각이라
    UTC 로 변환해도 같은 날짜가 된다."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except Exception:
        return None


# ==================================================================
# 국내 — KRX 전종목 일괄 (기본 비활성화)
# ==================================================================
KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
USE_KRX_BULK = False

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
# 국내 — 네이버 금융
# ==================================================================
def _fetch_naver_one(symbol: str, target: date, log):
    """기준일 이하 마지막 종가와 그 거래일을 (값, 날짜) 로 돌려준다."""
    start = (target - timedelta(days=20)).strftime("%Y%m%d")
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
            if not close or close <= 0:
                continue
            d = None
            try:
                ds = str(row[0]).strip()
                d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
            except Exception:
                d = None
            if d and d > target:
                continue
            return close, d
    except Exception as exc:
        log(f"네이버 {symbol} 실패: {exc}")
    return None, None


def _fetch_naver_many(symbols, target, log):
    out, dates = {}, {}
    for s in symbols:
        v, d = _fetch_naver_one(s, target, log)
        if v:
            out[s] = v
            if d:
                dates[s] = d
        time.sleep(0.05)
    return out, dates


# ==================================================================
# 해외 · 환율 — ① 야후 차트 API  ② Stooq CSV
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
    """기준일 이하 마지막 종가와 그 거래일을 (값, 날짜) 로 돌려준다."""
    sym = _yahoo_symbol(ticker)
    p1 = int(time.mktime((target - timedelta(days=30)).timetuple()))
    p2 = int(time.mktime((target + timedelta(days=2)).timetuple()))
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
            node = res[0]
            closes = (node.get("indicators", {})
                      .get("quote", [{}])[0].get("close") or [])
            stamps = node.get("timestamp") or []
            for i in range(len(closes) - 1, -1, -1):
                c = closes[i]
                if not c:
                    continue
                d = _ts_date(stamps[i]) if i < len(stamps) else None
                if d and d > target:        # 기준일 이후 봉은 건너뛴다
                    continue
                return float(c), d
        except Exception as exc:
            log(f"야후 {sym} 실패: {exc}")
    return None, None


def _stooq_symbol(ticker: str) -> str:
    if ticker == "BENCH-SP500":
        return "^spx"
    if ticker == "FX-USDKRW":
        return "usdkrw"
    return f"{ticker.lower()}.us"


def _fetch_stooq_one(sym: str, target: date, log):
    """기준일 이하 마지막 종가와 그 거래일을 (값, 날짜) 로 돌려준다."""
    d1 = (target - timedelta(days=30)).strftime("%Y%m%d")
    d2 = target.strftime("%Y%m%d")
    url = f"https://stooq.com/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.lower().startswith("no data"):
            return None, None
        lines = text.splitlines()
        header = [h.strip().lower() for h in lines[0].split(",")]
        if "close" not in header:
            return None, None
        ci = header.index("close")
        di = header.index("date") if "date" in header else None
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) <= ci:
                continue
            v = _num(parts[ci])
            if not v or v <= 0:
                continue
            d = None
            if di is not None and len(parts) > di:
                try:
                    d = date.fromisoformat(parts[di].strip())
                except Exception:
                    d = None
            if d and d > target:
                continue
            return v, d
    except Exception as exc:
        log(f"Stooq {sym} 실패: {exc}")
    return None, None


def _fetch_overseas(tickers, target, log):
    """야후 → Stooq 순으로 시도하고, 실제 거래일도 함께 모은다."""
    out, dates, failed = {}, {}, []
    for t in tickers:
        v, d = _fetch_yahoo_one(t, target, log)
        if v is None:
            v, d = _fetch_stooq_one(_stooq_symbol(t), target, log)
        if v:
            out[t] = v
            if d:
                dates[t] = d
        else:
            failed.append(t)
        time.sleep(0.08)
    if failed:
        log(f"해외 {len(failed)}종목 수집 실패: {', '.join(failed)}")
    return out, dates


def _fetch_naver_fx(target, log):
    """네이버 시장지표 환율(매매기준율) 차트."""
    s = (target - timedelta(days=25)).strftime("%Y%m%d") + "0000"
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
            if not v or v <= 0:
                continue
            d = None
            try:
                ds = str(row.get("localTradedAt") or "")[:10]
                d = date.fromisoformat(ds)
            except Exception:
                d = None
            if d and d > target:
                continue
            return v, d
    except Exception as exc:
        log(f"네이버 환율 실패: {exc}")
    return None, None


def _fetch_fx(target, log):
    """원달러 환율. 야후 → 네이버 → Stooq 순. (값, 날짜) 반환."""
    v, d = _fetch_yahoo_one("FX-USDKRW", target, log)
    if v:
        return v, d
    v, d = _fetch_naver_fx(target, log)
    if v:
        return v, d
    return _fetch_stooq_one("usdkrw", target, log)


# ==================================================================
# 가상자산 — 업비트
# ==================================================================
def _fetch_upbit(markets, target: date, log):
    """KST 자정 마감 일봉. 날짜를 지정해 조회하므로 항상 해당일 값이다."""
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
        logs            : 안내·경고 메시지. "⚠" 로 시작하면 중요한 경고다.
        missing         : 수집 실패 종목명 (사람이 읽는 용도)
        missing_tickers : 수집 실패 티커 (수동 입력 UI 용도)
    """
    logs = []
    log = logs.append

    kr = [t for t, _, c, _, _ in ALL_TICKERS if c in ("kr_stock", "kr_etf")]
    us = [t for t, _, c, _, _ in ALL_TICKERS if c == "us"]
    crypto = [t for t, _, c, _, _ in ALL_TICKERS if c == "crypto"]

    prices = {}
    kr_dates, us_dates = {}, {}

    # 1) 국내 일괄 (KRX). 기본값은 꺼져 있고 네이버가 전량을 담당한다.
    if USE_KRX_BULK:
        prices.update(_fetch_krx_bulk(target, log))

    # 2) 국내 — 네이버
    need = [t for t in kr if t not in prices]
    want_kospi = "BENCH-KOSPI" not in prices
    if need or want_kospi:
        syms = need + (["KOSPI"] if want_kospi else [])
        got, gdates = _fetch_naver_many(syms, target, log)
        for k, v in got.items():
            prices["BENCH-KOSPI" if k == "KOSPI" else k] = v
        for k, d in gdates.items():
            kr_dates["BENCH-KOSPI" if k == "KOSPI" else k] = d

    # 3) 해외 + S&P500
    yh, us_dates = _fetch_overseas(us + ["BENCH-SP500"], target, log)
    prices.update(yh)

    # 4) 가상자산
    prices.update(_fetch_upbit(crypto, target, log))

    # 5) 환율
    fx, fx_date = _fetch_fx(target, log)
    if not fx:
        log("⚠ 환율을 가져오지 못했습니다. 해외 종목 평가가 비어 있을 수 있습니다.")

    # ---- 날짜 검증: 요청한 날짜와 실제 거래일이 다른 경우 경고 ----
    def _stale_note(dates_map, 구분):
        off = sorted({d for d in dates_map.values() if d and d != target})
        if not off:
            return
        n = sum(1 for d in dates_map.values() if d and d != target)
        log(f"⚠ {구분} {n}개가 요청하신 {target} 가 아니라 "
            f"{', '.join(str(d) for d in off)} 종가입니다. "
            f"그대로 저장되므로, 날짜를 맞추려면 아래 안내대로 다시 불러오세요.")

    _stale_note(kr_dates, "국내 종목")
    _stale_note(us_dates, "해외 종목")

    if us_dates and any(d and d < target for d in us_dates.values()):
        log("⚠ 미국 종가는 한국시간 기준 **다음 날 아침 7시 이후**에 확정됩니다. "
            f"{target} 종가가 필요하면 "
            f"{(target + timedelta(days=1)).isoformat()} 오전 7시 이후에 "
            "같은 날짜로 다시 불러오시면 덮어써집니다.")

    if fx and fx_date and fx_date != target:
        log(f"⚠ 환율이 {target} 가 아니라 {fx_date} 값입니다.")

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
