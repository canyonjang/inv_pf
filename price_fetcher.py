"""
종가 수집 모듈
--------------
교수 화면의 [기준가 업데이트] 버튼을 눌렀을 때만 호출됩니다.
자동 실행·주기적 폴링은 어디에도 없습니다.

수집 소스 (모두 무료 · API 키 불필요)
    국내 주식·ETF, 코스피지수 : pykrx  (한국거래소)
    해외 주식·ETF, S&P500, 환율 : yfinance
    가상자산                    : 업비트 공개 API

호출 1회당 외부 요청 수
    pykrx  2~3회 (전 종목을 통째로 받아 필요한 것만 골라 씀)
    yfinance 2회 (해외 종목 일괄 + 지수/환율)
    업비트  종목 수만큼 (보통 4회)
"""

from datetime import date, timedelta

import requests

from universe import ALL_TICKERS, SEC_MAP


# ------------------------------------------------------------------
# 기준일 계산
# ------------------------------------------------------------------
def previous_business_day(base: date | None = None) -> date:
    """전일(주말이면 직전 금요일)을 돌려준다."""
    d = (base or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:          # 5=토, 6=일
        d -= timedelta(days=1)
    return d


# ------------------------------------------------------------------
# 국내 (pykrx)
# ------------------------------------------------------------------
def _fetch_krx(codes, target: date, log):
    result = {}
    if not codes:
        return result
    try:
        from pykrx import stock
    except ImportError:
        log("pykrx 미설치 — 국내 종목 수집 건너뜀")
        return result

    ymd = target.strftime("%Y%m%d")

    for fn_name in ("get_market_ohlcv", "get_etf_ohlcv_by_ticker"):
        fn = getattr(stock, fn_name, None)
        if fn is None:
            continue
        try:
            df = fn(ymd)
        except Exception as exc:
            log(f"KRX {fn_name} 실패: {exc}")
            continue
        if df is None or df.empty:
            continue
        for code in codes:
            if code in result:
                continue
            if code in df.index:
                try:
                    close = float(df.loc[code, "종가"])
                except Exception:
                    continue
                if close > 0:
                    result[code] = close

    # 코스피 지수
    try:
        idx = stock.get_index_ohlcv(ymd, ymd, "1001")
        if idx is not None and not idx.empty:
            result["BENCH-KOSPI"] = float(idx.iloc[-1]["종가"])
    except Exception as exc:
        log(f"코스피지수 수집 실패: {exc}")

    return result


# ------------------------------------------------------------------
# 해외 (yfinance)
# ------------------------------------------------------------------
def _fetch_yahoo(tickers, target: date, log):
    """해외 종목 + S&P500 + 원달러 환율을 두 번의 요청으로 받는다."""
    result, fx = {}, None
    try:
        import yfinance as yf
    except ImportError:
        log("yfinance 미설치 — 해외 종목 수집 건너뜀")
        return result, fx

    start = (target - timedelta(days=12)).isoformat()
    end = (target + timedelta(days=1)).isoformat()

    def last_close(df, col=None):
        try:
            s = df[col]["Close"] if col else df["Close"]
            s = s.dropna()
            return float(s.iloc[-1]) if len(s) else None
        except Exception:
            return None

    # (1) 해외 개별 종목 일괄
    if tickers:
        try:
            data = yf.download(" ".join(tickers), start=start, end=end,
                               progress=False, auto_adjust=False,
                               group_by="ticker", threads=False)
            for t in tickers:
                v = last_close(data, t if len(tickers) > 1 else None)
                if v:
                    result[t] = v
        except Exception as exc:
            log(f"해외 종목 수집 실패: {exc}")

    # (2) 지수 + 환율 일괄
    try:
        data2 = yf.download("^GSPC USDKRW=X", start=start, end=end,
                            progress=False, auto_adjust=False,
                            group_by="ticker", threads=False)
        v = last_close(data2, "^GSPC")
        if v:
            result["BENCH-SP500"] = v
        fx = last_close(data2, "USDKRW=X")
    except Exception as exc:
        log(f"지수·환율 수집 실패: {exc}")

    return result, fx


# ------------------------------------------------------------------
# 가상자산 (업비트)
# ------------------------------------------------------------------
def _fetch_upbit(markets, target: date, log):
    """KST 자정 마감 일봉의 종가를 가져온다."""
    result = {}
    to_param = f"{target.isoformat()}T23:59:59+09:00"
    for market in markets:
        try:
            resp = requests.get(
                "https://api.upbit.com/v1/candles/days",
                params={"market": market, "to": to_param, "count": 1},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                result[market] = float(rows[0]["trade_price"])
        except Exception as exc:
            log(f"{market} 수집 실패: {exc}")
    return result


# ------------------------------------------------------------------
# 통합
# ------------------------------------------------------------------
def fetch_all(target: date):
    """
    모든 종목의 종가를 수집한다.

    반환: (rows, fx, logs, missing)
        rows    : Supabase inv_pf_prices 에 그대로 넣을 dict 리스트
        fx      : 원달러 환율 (float 또는 None)
        logs    : 경고 메시지 리스트
        missing : 수집 실패한 종목명 리스트
    """
    logs = []
    def log(msg):
        logs.append(msg)

    krx_codes = [t for t, _, c, _, _ in ALL_TICKERS if c in ("kr_stock", "kr_etf")]
    us_tickers = [t for t, _, c, _, _ in ALL_TICKERS if c == "us"]
    crypto = [t for t, _, c, _, _ in ALL_TICKERS if c == "crypto"]

    prices = {}
    prices.update(_fetch_krx(krx_codes, target, log))
    yh, fx = _fetch_yahoo(us_tickers, target, log)
    prices.update(yh)
    prices.update(_fetch_upbit(crypto, target, log))

    rows, missing = [], []
    for ticker, name, cls, _high, cur in ALL_TICKERS:
        price = prices.get(ticker)
        if price is None:
            missing.append(f"{name}({ticker})")
            continue
        rows.append({
            "ticker": ticker,
            "price_date": target.isoformat(),
            "close_price": round(float(price), 4),
            "fx_usdkrw": round(float(fx), 2) if fx else None,
        })

    return rows, fx, logs, missing
