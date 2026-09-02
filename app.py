"""
투자 포트폴리오 설계 및 성과평가 · 라이브 웹앱
Streamlit + Supabase

배포 전 준비
 1) Supabase에서 supabase_schema.sql 실행
 2) Streamlit Cloud > Settings > Secrets 에 SUPABASE_URL / SUPABASE_KEY / PROF_PW 입력
 3) requirements.txt 의 패키지 설치

설계 원칙
 · 자동 새로고침·폴링 없음. 모든 갱신은 버튼을 눌렀을 때만 일어남.
 · 외부 시세 API 호출은 교수 화면의 [기준가 업데이트] 버튼에서만 발생.
 · 조회 결과는 세션 캐시에 담아 두고, 새로고침 버튼을 눌러야 다시 읽음.
"""

import io
import json
from datetime import date, datetime

import pandas as pd
import streamlit as st
from supabase import Client, create_client

from universe import (SEC_MAP, TRADABLE, group_of, is_high_risk, label)
from price_fetcher import fetch_all, previous_business_day

st.set_page_config(page_title="투자 포트폴리오", page_icon="📈", layout="wide")

# ==========================================================
# 상수
# ==========================================================
CLASSES = ["인하대", "숙대1", "숙대2"]
PHASES = ["대기", "주문접수", "체결대기", "체결완료", "종료"]

INITIAL_CAPITAL = 1000.0        # 만원
MIN_HOLDINGS, MAX_HOLDINGS = 4, 7
MAX_SINGLE_WEIGHT = 0.30
MAX_HIGHRISK_WEIGHT = 0.20

ROUNDS = {
    1: ("최초 매수",      "7주차"),
    2: ("1차 리밸런싱",   "9주차"),
    3: ("2차 리밸런싱",   "10주차"),
    4: ("3차 리밸런싱",   "11주차"),
    5: ("4차 리밸런싱",   "12주차"),
    6: ("최종 결과 확인", "13주차"),
}
TRADING_ROUNDS = [1, 2, 3, 4, 5]

BIT_TYPES = ["(선택 안 함)", "보존가", "추종자", "독립가", "축적가"]


# ==========================================================
# Supabase
# ==========================================================
@st.cache_resource
def init_connection() -> Client:
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


supabase: Client = init_connection()
PROF_PW = st.secrets.get("PROF_PW", "3383")


def clear_all_cache():
    """버튼을 눌렀을 때만 호출. 다음 조회에서 DB를 다시 읽는다."""
    for fn in (_q_status, _q_students, _q_prices, _q_orders,
               _q_reflections, _q_snapshots):
        fn.clear()


# --- 조회 함수. ttl 을 길게 두어 화면 재실행만으로는 DB를 때리지 않는다. ---
@st.cache_data(ttl=600, show_spinner=False)
def _q_status(class_name):
    return supabase.table("inv_pf_status").select("*") \
        .eq("class_name", class_name).execute().data


@st.cache_data(ttl=600, show_spinner=False)
def _q_students(class_name):
    return supabase.table("inv_pf_students").select("*") \
        .eq("class_name", class_name).execute().data


@st.cache_data(ttl=600, show_spinner=False)
def _q_prices():
    return supabase.table("inv_pf_prices").select("*") \
        .order("price_date").execute().data


@st.cache_data(ttl=600, show_spinner=False)
def _q_orders(class_name):
    return supabase.table("inv_pf_orders").select("*") \
        .eq("class_name", class_name).order("id").execute().data


@st.cache_data(ttl=600, show_spinner=False)
def _q_reflections(class_name):
    return supabase.table("inv_pf_reflections").select("*") \
        .eq("class_name", class_name).execute().data


@st.cache_data(ttl=600, show_spinner=False)
def _q_snapshots(class_name):
    return supabase.table("inv_pf_snapshots").select("*") \
        .eq("class_name", class_name).order("price_date").execute().data


def get_status(class_name) -> dict:
    rows = _q_status(class_name)
    if not rows:
        supabase.table("inv_pf_status").insert({"class_name": class_name}).execute()
        _q_status.clear()
        return {"class_name": class_name, "current_round": 0,
                "phase": "대기", "last_price_date": None}
    return rows[0]


def set_status(class_name, **kwargs):
    kwargs["updated_at"] = datetime.utcnow().isoformat()
    supabase.table("inv_pf_status").update(kwargs) \
        .eq("class_name", class_name).execute()
    _q_status.clear()


# ==========================================================
# 가격 유틸
# ==========================================================
def price_table() -> pd.DataFrame:
    rows = _q_prices()
    if not rows:
        return pd.DataFrame(columns=["ticker", "price_date", "close_price", "fx_usdkrw"])
    df = pd.DataFrame(rows)
    df["price_date"] = pd.to_datetime(df["price_date"]).dt.date
    df["close_price"] = df["close_price"].astype(float)
    df["fx_usdkrw"] = pd.to_numeric(df["fx_usdkrw"], errors="coerce")
    return df


def to_krw(ticker, close, fx):
    """USD 표시 종목은 원화로 환산한다."""
    cur = SEC_MAP.get(ticker, {}).get("currency", "KRW")
    if cur == "USD":
        if not fx or pd.isna(fx):
            return None
        return float(close) * float(fx)
    return float(close)


def price_map_on(df: pd.DataFrame, on_date: date) -> dict:
    """특정 날짜의 {티커: 원화가격}. 그 날짜가 없으면 빈 dict."""
    sub = df[df["price_date"] == on_date]
    out = {}
    for _, r in sub.iterrows():
        p = to_krw(r["ticker"], r["close_price"], r["fx_usdkrw"])
        if p:
            out[r["ticker"]] = p
    return out


def available_dates(df: pd.DataFrame):
    return sorted(df["price_date"].unique()) if not df.empty else []


# ==========================================================
# 포트폴리오 계산
# ==========================================================
def positions_from(orders, upto_round=None):
    """체결된 주문만으로 보유수량과 현금을 재구성한다."""
    qty, cash = {}, INITIAL_CAPITAL
    for o in sorted(orders, key=lambda x: (x["round_no"], x["id"])):
        if o.get("status") != "executed":
            continue
        if upto_round is not None and o["round_no"] > upto_round:
            continue
        t, q = o["ticker"], float(o.get("exec_qty") or 0)
        px = float(o.get("exec_price_krw") or 0)
        if o["side"] == "buy":
            cash -= q * px / 10000
            qty[t] = qty.get(t, 0.0) + q
        else:
            cash += q * px / 10000
            qty[t] = qty.get(t, 0.0) - q
    qty = {t: q for t, q in qty.items() if q > 1e-9}
    return qty, round(cash, 4)


def valuate(qty, cash, pmap):
    """보유수량을 특정 시점 가격으로 평가한다. (단위: 만원)"""
    rows, total = [], float(cash)
    for t, q in qty.items():
        px = pmap.get(t)
        if px is None:
            rows.append({"ticker": t, "qty": q, "price": None, "value": None})
            continue
        v = q * px / 10000
        total += v
        rows.append({"ticker": t, "qty": q, "price": px, "value": v})
    for r in rows:
        r["weight"] = (r["value"] / total) if r["value"] and total else None
    return rows, total


def student_orders(all_orders, name):
    return [o for o in all_orders if o["name"] == name]


# ==========================================================
# 로그인
# ==========================================================
if "role" not in st.session_state:
    st.title("📈 투자 포트폴리오 설계 및 성과평가")
    st.caption("가상 투자금 1,000만원 · 6주간의 실전 기록")
    role = st.radio("접속 유형", ["학생", "교수"], horizontal=True)

    if role == "학생":
        st.info("교수님이 강의실을 열면 이름을 입력해 입장할 수 있습니다.")
        c1, c2 = st.columns(2)
        name = c1.text_input("이름")
        student_no = c2.text_input("학번")
        with st.expander("1주차 검사 결과 (선택 · 나중에 성찰 자료로 씁니다)"):
            bit = st.selectbox("BIT 투자자 유형", BIT_TYPES)
            risk = st.number_input("위험수용도 총점 (13~47)", 0, 47, 0)

        if st.button("입장하기", type="primary", use_container_width=True):
            if not name.strip():
                st.error("이름을 입력해주세요.")
            else:
                rows = supabase.table("inv_pf_status").select("*").execute().data
                active = [r["class_name"] for r in rows if r["phase"] != "대기"]
                if len(active) != 1:
                    st.error("열려 있는 강의실이 없거나 여러 곳이 동시에 열려 있습니다. "
                             "교수님께 문의하세요.")
                else:
                    cn = active[0]
                    payload = {"class_name": cn, "name": name.strip(),
                               "student_no": student_no.strip() or None}
                    if bit != BIT_TYPES[0]:
                        payload["bit_type"] = bit
                    if risk:
                        payload["risk_score"] = int(risk)
                    try:
                        supabase.table("inv_pf_students").upsert(
                            payload, on_conflict="class_name,name").execute()
                    except Exception:
                        pass
                    _q_students.clear()
                    st.session_state.update(role="student", name=name.strip(),
                                            class_name=cn)
                    st.rerun()
    else:
        cn = st.selectbox("분반 선택", CLASSES)
        pw = st.text_input("비밀번호", type="password")
        if st.button("교수 통제소 입장", type="primary"):
            if pw == PROF_PW:
                st.session_state.update(role="professor", class_name=cn)
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
    st.stop()


# ==========================================================
# 공통 헤더
# ==========================================================
my_class = st.session_state.class_name
status = get_status(my_class)
rnd = status.get("current_round") or 0
phase = status.get("phase") or "대기"
last_pd = status.get("last_price_date")
if isinstance(last_pd, str):
    last_pd = datetime.strptime(last_pd, "%Y-%m-%d").date()

rinfo = ROUNDS.get(rnd)

h1, h2 = st.columns([8, 2])
with h1:
    if rinfo:
        st.markdown(f"### 🏫 [{my_class}] · 라운드 {rnd}. {rinfo[0]} ({rinfo[1]})")
    else:
        st.markdown(f"### 🏫 [{my_class}] 강의실")
    st.caption(f"단계: **{phase}** · 기준가 일자: "
               f"**{last_pd.isoformat() if last_pd else '아직 없음'}**")
with h2:
    if st.button("로그아웃"):
        st.session_state.clear()
        st.rerun()
st.write("---")

pdf = price_table()
pmap_now = price_map_on(pdf, last_pd) if last_pd else {}


# ==========================================================
# 학생 화면
# ==========================================================
if st.session_state.role == "student":
    me = st.session_state.name

    if st.button("🔄 화면 새로고침", type="primary", use_container_width=True):
        clear_all_cache()
        st.rerun()

    if rnd == 0 or phase == "대기":
        st.info(f"{me}님, 접속되었습니다. 교수님이 라운드를 열 때까지 기다려 주세요.")
        st.stop()

    all_orders = _q_orders(my_class)
    my_orders = student_orders(all_orders, me)
    qty, cash = positions_from(my_orders)
    rows, total = valuate(qty, cash, pmap_now)
    ret_pct = (total / INITIAL_CAPITAL - 1) * 100

    # ---------------- 현재 평가 ----------------
    st.subheader("① 나의 현재 포트폴리오")
    if not qty and not any(o["round_no"] == 1 for o in my_orders):
        st.warning("아직 최초 매수를 하지 않았습니다.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("평가금액", f"{total:,.1f} 만원", delta=f"{total - INITIAL_CAPITAL:+,.1f} 만원")
        m2.metric("수익률", f"{ret_pct:+.2f} %")
        m3.metric("현금", f"{cash:,.1f} 만원")

        if rows:
            disp = pd.DataFrame([{
                "종목": label(r["ticker"]),
                "수량": f"{r['qty']:,.4f}".rstrip("0").rstrip("."),
                "현재가(원)": f"{r['price']:,.0f}" if r["price"] else "—",
                "평가액(만원)": f"{r['value']:,.1f}" if r["value"] else "—",
                "비중": f"{r['weight']*100:.1f}%" if r["weight"] else "—",
            } for r in rows])
            st.dataframe(disp, use_container_width=True, hide_index=True)
            hr = sum(r["value"] or 0 for r in rows if is_high_risk(r["ticker"]))
            st.caption(f"고위험자산 비중 {hr/total*100:.1f}% (상한 20%) · "
                       f"기준가 {last_pd.isoformat() if last_pd else '—'}")

    st.write("---")

    # ---------------- 주문 ----------------
    refl_rows = [r for r in _q_reflections(my_class) if r["name"] == me]
    done_rounds = {r["round_no"] for r in refl_rows}

    if phase == "주문접수" and rnd in TRADING_ROUNDS:
        st.subheader(f"② 라운드 {rnd} 주문")

        if rnd in done_rounds:
            st.success("이번 라운드 제출을 마쳤습니다. 수정할 수 없습니다.")
            mine_r = [o for o in my_orders if o["round_no"] == rnd]
            if mine_r:
                st.write("**제출한 주문**")
                for o in mine_r:
                    if o["side"] == "buy":
                        st.write(f"· 매수 — {label(o['ticker'])} {o['amount_manwon']:,.0f}만원")
                    else:
                        st.write(f"· 매도 — {label(o['ticker'])} "
                                 f"보유분의 {float(o['sell_ratio'])*100:.0f}%")
            else:
                st.write("· 이번 라운드는 **거래하지 않음**으로 제출했습니다.")
        else:
            st.error("**제출은 한 번뿐입니다.** 제출 후에는 수정할 수 없습니다.")
            st.caption("체결가는 지금 보이는 가격이 아니라 **다음 기준가 업데이트 시점의 종가**입니다.")

            # 라운드 1은 최초 매수라 '지금 화면을 보고 든 생각'이 성립하지 않는다.
            # 2~5 라운드에서만 손익을 본 직후의 반응을 받는다.
            feeling = ""
            if rnd != 1:
                feeling = st.text_area(
                    "먼저 — 지금 이 화면을 보고 든 생각을 한두 문장으로 적어주세요. *(필수)*",
                    height=80, key=f"feel{rnd}",
                    placeholder="예: 반도체가 많이 빠져서 불안한데, 팔아야 할지 더 사야 할지 모르겠다.")

            # 심정을 적기 전에는 주문 화면이 열리지 않는다.
            # (st.stop() 을 쓰지 않으므로 아래 기록·다운로드 영역은 그대로 보인다)
            if rnd != 1 and not feeling.strip():
                st.info("위 칸을 채우면 주문 화면이 열립니다.")

            # ---------- 라운드 1: 최초 배분 ----------
            elif rnd == 1:
                st.write(f"**1,000만원을 {MIN_HOLDINGS}~{MAX_HOLDINGS}개 종목에 배분하세요.**")
                st.caption(f"단일 종목 최대 {MAX_SINGLE_WEIGHT*100:.0f}% "
                           f"· 고위험(⚠️) 합계 최대 {MAX_HIGHRISK_WEIGHT*100:.0f}%")

                groups = {}
                for t in TRADABLE:
                    groups.setdefault(group_of(t), []).append(t)

                picked = []
                for g, ts in groups.items():
                    sel = st.multiselect(g, ts, format_func=label, key=f"g{g}")
                    picked += sel

                if picked:
                    st.write("**금액 배분 (만원)**")
                    alloc = {}
                    cols = st.columns(min(len(picked), 3))
                    for i, t in enumerate(picked):
                        alloc[t] = cols[i % len(cols)].number_input(
                            label(t), 0, 1000, 0, step=50, key=f"a{t}")
                    used = sum(alloc.values())
                    hrsum = sum(v for t, v in alloc.items() if is_high_risk(t))
                    st.info(f"배분 합계 **{used:,} / 1,000만원** · "
                            f"고위험 **{hrsum:,}만원 ({hrsum/10:.0f}%)**")

                    errs = []
                    if not (MIN_HOLDINGS <= len([t for t in picked if alloc[t] > 0]) <= MAX_HOLDINGS):
                        errs.append(f"금액을 배정한 종목이 {MIN_HOLDINGS}~{MAX_HOLDINGS}개여야 합니다.")
                    if used > INITIAL_CAPITAL:
                        errs.append("배분 합계가 1,000만원을 넘습니다.")
                    for t, v in alloc.items():
                        if v > INITIAL_CAPITAL * MAX_SINGLE_WEIGHT:
                            errs.append(f"{label(t)}이(가) 30%(300만원)를 초과합니다.")
                    if hrsum > INITIAL_CAPITAL * MAX_HIGHRISK_WEIGHT:
                        errs.append("고위험자산 합계가 20%(200만원)를 초과합니다.")

                    reason = st.text_area(
                        "각 상품을 고른 이유와, 앞으로 지킬 리밸런싱 원칙을 적어주세요. *(필수)*",
                        height=140, key=f"why{rnd}")

                    for e in errs:
                        st.error(e)

                    if st.button("🔒 이 포트폴리오로 확정 제출", type="primary",
                                 use_container_width=True,
                                 disabled=bool(errs) or not reason.strip()):
                        payload = [{
                            "class_name": my_class, "name": me, "round_no": rnd,
                            "ticker": t, "side": "buy", "amount_manwon": float(v),
                            "status": "pending",
                        } for t, v in alloc.items() if v > 0]
                        supabase.table("inv_pf_orders").insert(payload).execute()
                        supabase.table("inv_pf_reflections").upsert({
                            "class_name": my_class, "name": me, "round_no": rnd,
                            "feeling": feeling.strip() or None, "reason": reason.strip(),
                            "no_trade": False,
                        }, on_conflict="class_name,name,round_no").execute()
                        clear_all_cache()
                        st.success("제출 완료!")
                        st.rerun()

            # ---------- 라운드 2~5: 리밸런싱 ----------
            else:
                if not qty:
                    st.warning("보유 종목이 없어 매도할 수 없습니다. 매수만 가능합니다.")

                no_trade = st.checkbox("이번 라운드는 **거래하지 않겠습니다**", key=f"nt{rnd}")

                sells, buys = {}, {}
                if not no_trade:
                    if qty:
                        st.write("**매도 — 보유분의 몇 %를 팔까요?**")
                        for t in qty:
                            sells[t] = st.select_slider(
                                label(t), [0, 25, 50, 75, 100], 0, key=f"s{t}{rnd}")
                        st.write("")

                    st.write("**매수 — 금액(만원)**")
                    cand = st.multiselect("매수할 종목", TRADABLE, format_func=label,
                                          key=f"bs{rnd}")
                    if cand:
                        cols = st.columns(min(len(cand), 3))
                        for i, t in enumerate(cand):
                            buys[t] = cols[i % len(cols)].number_input(
                                label(t), 0, 1000, 0, step=25, key=f"b{t}{rnd}")

                # --- 예상 검증 (현재가 기준 추정) ---
                errs = []
                if not no_trade:
                    est_qty = dict(qty)
                    est_cash = cash
                    for t, pct in sells.items():
                        if pct:
                            px = pmap_now.get(t)
                            if px:
                                dq = qty[t] * pct / 100
                                est_qty[t] = qty[t] - dq
                                est_cash += dq * px / 10000
                    spend = sum(buys.values())
                    if spend > est_cash + 1e-6:
                        errs.append(f"매수 금액({spend:,.0f}만원)이 "
                                    f"가용 현금({est_cash:,.1f}만원)을 넘습니다.")
                    for t, v in buys.items():
                        if v:
                            px = pmap_now.get(t)
                            if px:
                                est_qty[t] = est_qty.get(t, 0) + v * 10000 / px
                            est_cash -= v

                    er, etot = valuate({k: v for k, v in est_qty.items() if v > 1e-9},
                                       est_cash, pmap_now)
                    n_hold = len([r for r in er if r["value"]])
                    if etot > 0:
                        for r in er:
                            if r["value"] and r["value"] / etot > MAX_SINGLE_WEIGHT + 1e-6:
                                errs.append(f"{label(r['ticker'])} 비중이 30%를 넘습니다 "
                                            f"({r['value']/etot*100:.1f}%).")
                        hr = sum(r["value"] or 0 for r in er if is_high_risk(r["ticker"]))
                        if hr / etot > MAX_HIGHRISK_WEIGHT + 1e-6:
                            errs.append(f"고위험자산 비중이 20%를 넘습니다 ({hr/etot*100:.1f}%).")
                    if n_hold and not (MIN_HOLDINGS <= n_hold <= MAX_HOLDINGS):
                        errs.append(f"보유 종목 수가 {MIN_HOLDINGS}~{MAX_HOLDINGS}개를 벗어납니다 "
                                    f"({n_hold}개).")
                    if not sum(sells.values()) and not spend:
                        errs.append("매매 내용이 없습니다. 거래하지 않으려면 위 체크박스를 선택하세요.")

                reason = st.text_area(
                    "이 결정을 내린 이유를 적어주세요. *(필수 · 거래하지 않는 것도 결정입니다)*",
                    height=120, key=f"why{rnd}")

                for e in errs:
                    st.error(e)
                if not errs and not no_trade:
                    st.caption("※ 위 검증은 현재가 기준 추정입니다. 실제 체결가는 다음 종가입니다.")

                if st.button("🔒 확정 제출", type="primary", use_container_width=True,
                             disabled=bool(errs) or not reason.strip()):
                    payload = []
                    if not no_trade:
                        for t, pct in sells.items():
                            if pct:
                                payload.append({
                                    "class_name": my_class, "name": me, "round_no": rnd,
                                    "ticker": t, "side": "sell",
                                    "sell_ratio": pct / 100, "status": "pending"})
                        for t, v in buys.items():
                            if v:
                                payload.append({
                                    "class_name": my_class, "name": me, "round_no": rnd,
                                    "ticker": t, "side": "buy",
                                    "amount_manwon": float(v), "status": "pending"})
                    if payload:
                        supabase.table("inv_pf_orders").insert(payload).execute()
                    supabase.table("inv_pf_reflections").upsert({
                        "class_name": my_class, "name": me, "round_no": rnd,
                        "feeling": feeling.strip(), "reason": reason.strip(),
                        "no_trade": bool(no_trade),
                    }, on_conflict="class_name,name,round_no").execute()
                    clear_all_cache()
                    st.success("제출 완료!")
                    st.rerun()

    elif phase in ("체결대기", "체결완료"):
        st.subheader("② 주문 마감")
        st.info("이번 라운드 주문이 마감되었습니다. "
                "체결 결과는 다음 수업 전 기준가 업데이트에 반영됩니다.")
    elif rnd == 6:
        st.subheader("② 투자 종료")
        st.success("6주간의 투자가 끝났습니다. 아래에서 기록을 내려받아 "
                   "기말 보고서 작성에 사용하세요.")

    # ---------------- 나의 기록 ----------------
    st.write("---")
    st.subheader("③ 나의 6주 기록")

    snaps = [s for s in _q_snapshots(my_class) if s["name"] == me]
    if snaps:
        sdf = pd.DataFrame(snaps)
        sdf["price_date"] = pd.to_datetime(sdf["price_date"])
        sdf = sdf.sort_values("price_date")
        sdf["수익률(%)"] = (sdf["total_value"].astype(float) / INITIAL_CAPITAL - 1) * 100
        st.line_chart(sdf.set_index("price_date")["수익률(%)"], height=220)

    refl_map = {r["round_no"]: r for r in refl_rows}
    for r_no in sorted(set(list(refl_map.keys()))):
        r = refl_map[r_no]
        nm = ROUNDS.get(r_no, (f"라운드 {r_no}", ""))[0]
        with st.expander(f"라운드 {r_no} · {nm}"):
            st.write(f"**그때의 심정** — {r.get('feeling') or '—'}")
            st.write(f"**결정 이유** — {r.get('reason') or '—'}")
            mine_r = [o for o in my_orders if o["round_no"] == r_no]
            if not mine_r:
                st.write("**주문** — 거래 없음")
            for o in mine_r:
                if o["side"] == "buy":
                    txt = f"매수 {label(o['ticker'])} {o['amount_manwon']:,.0f}만원"
                else:
                    txt = f"매도 {label(o['ticker'])} 보유분의 {float(o['sell_ratio'])*100:.0f}%"
                if o["status"] == "executed":
                    txt += (f" → 체결 {float(o['exec_price_krw']):,.0f}원 "
                            f"× {float(o['exec_qty']):,.4f} ({o['exec_date']})")
                else:
                    txt += " → 체결 대기"
                st.write("· " + txt)

    # ---------------- 다운로드 ----------------
    st.write("---")
    st.subheader("④ 기말 보고서용 기록 내려받기")
    st.caption("휴대폰에서도 바로 저장됩니다. 파일을 열어 발표자료에 붙여 쓰세요.")

    trade_rows = []
    for o in sorted(my_orders, key=lambda x: (x["round_no"], x["id"])):
        trade_rows.append({
            "라운드": o["round_no"],
            "구분": "매수" if o["side"] == "buy" else "매도",
            "종목": SEC_MAP.get(o["ticker"], {}).get("name", o["ticker"]),
            "티커": o["ticker"],
            "주문금액(만원)": o.get("amount_manwon"),
            "매도비율(%)": (float(o["sell_ratio"]) * 100) if o.get("sell_ratio") else None,
            "체결가(원)": o.get("exec_price_krw"),
            "체결수량": o.get("exec_qty"),
            "체결일": o.get("exec_date"),
            "상태": o.get("status"),
            "제출시각": o.get("submitted_at"),
        })
    trades_df = pd.DataFrame(trade_rows)

    snap_df = pd.DataFrame([{
        "기준일": s["price_date"],
        "평가금액(만원)": s["total_value"],
        "현금(만원)": s["cash"],
        "수익률(%)": round((float(s["total_value"]) / INITIAL_CAPITAL - 1) * 100, 2),
    } for s in sorted(snaps, key=lambda x: x["price_date"])])

    refl_df = pd.DataFrame([{
        "라운드": r["round_no"],
        "그때의 심정": r.get("feeling"),
        "결정 이유": r.get("reason"),
        "거래없음": r.get("no_trade"),
        "제출시각": r.get("submitted_at"),
    } for r in sorted(refl_rows, key=lambda x: x["round_no"])])

    buf = io.StringIO()
    buf.write(f"# {me} 투자 기록\n\n## 1. 거래내역\n")
    buf.write(trades_df.to_csv(index=False) if not trades_df.empty else "(없음)\n")
    buf.write("\n## 2. 주차별 평가금액\n")
    buf.write(snap_df.to_csv(index=False) if not snap_df.empty else "(없음)\n")
    buf.write("\n## 3. 라운드별 기록\n")
    buf.write(refl_df.to_csv(index=False) if not refl_df.empty else "(없음)\n")

    d1, d2 = st.columns(2)
    d1.download_button("📥 전체 기록 (CSV)", buf.getvalue().encode("utf-8-sig"),
                       file_name=f"투자기록_{me}.csv", mime="text/csv",
                       use_container_width=True)

    md = io.StringIO()
    md.write(f"# {me} · 6주 투자 기록\n\n")
    if not snap_df.empty:
        fin = snap_df.iloc[-1]
        md.write(f"- 최종 평가금액: {float(fin['평가금액(만원)']):,.1f}만원 "
                 f"({float(fin['수익률(%)']):+.2f}%)\n")
        vals = snap_df["평가금액(만원)"].astype(float)
        peak = vals.cummax()
        mdd = ((vals - peak) / peak).min() * 100
        md.write(f"- 최대낙폭(MDD): {mdd:.2f}%\n\n")
    for r in sorted(refl_rows, key=lambda x: x["round_no"]):
        nm = ROUNDS.get(r["round_no"], (f"라운드 {r['round_no']}", ""))[0]
        md.write(f"## 라운드 {r['round_no']} · {nm}\n")
        md.write(f"**그때의 심정**\n\n{r.get('feeling') or '—'}\n\n")
        md.write(f"**결정 이유**\n\n{r.get('reason') or '—'}\n\n")
        for o in [x for x in my_orders if x["round_no"] == r["round_no"]]:
            if o["side"] == "buy":
                md.write(f"- 매수 {SEC_MAP.get(o['ticker'],{}).get('name',o['ticker'])} "
                         f"{o['amount_manwon']:,.0f}만원\n")
            else:
                md.write(f"- 매도 {SEC_MAP.get(o['ticker'],{}).get('name',o['ticker'])} "
                         f"보유분의 {float(o['sell_ratio'])*100:.0f}%\n")
        md.write("\n")
    d2.download_button("📥 보고서 초안 (Markdown)", md.getvalue().encode("utf-8"),
                       file_name=f"투자기록_{me}.md", mime="text/markdown",
                       use_container_width=True)


# ==========================================================
# 교수 통제소
# ==========================================================
else:
    students = _q_students(my_class)
    all_orders = _q_orders(my_class)
    refls = _q_reflections(my_class)
    snaps = _q_snapshots(my_class)

    t1, t2, t3 = st.columns([2, 6, 2])
    if t1.button("🔄 현황 새로고침", type="primary"):
        clear_all_cache()
        st.rerun()
    t2.markdown(f"**라운드 {rnd} · {phase}**")
    t3.metric("등록 학생", f"{len(students)} 명")

    st.write("---")

    # ---------------- 진행 제어 ----------------
    c = st.columns([3, 3, 2])
    new_round = c[0].selectbox(
        "라운드", list(range(0, 7)), index=rnd,
        format_func=lambda x: "— 대기 —" if x == 0 else f"{x}. {ROUNDS[x][0]} ({ROUNDS[x][1]})")
    new_phase = c[1].selectbox("단계", PHASES, index=PHASES.index(phase))
    if c[2].button("✅ 적용", type="primary", use_container_width=True):
        set_status(my_class, current_round=new_round, phase=new_phase)
        clear_all_cache()
        st.rerun()

    st.write("---")

    # ---------------- 기준가 업데이트 ----------------
    st.subheader("💰 기준가 업데이트")
    st.caption("이 버튼을 누를 때만 외부 시세를 불러옵니다. "
               "수업 시작 전에 한 번, 체결 전날 밤에 한 번 누르시면 됩니다.")

    g1, g2 = st.columns([3, 2])
    target = g1.date_input("기준일 (기본: 전 영업일)", value=previous_business_day())
    go = g2.button("📥 전일 종가 불러오기", type="primary", use_container_width=True)

    if go:
        with st.spinner("시세를 불러오는 중입니다. 30~60초 걸립니다..."):
            rows, fx, logs, missing, missing_tk = fetch_all(target)

        # 환율을 못 가져왔으면 가장 최근에 저장된 환율을 그대로 쓴다.
        if not fx and not pdf.empty:
            prev = pdf["fx_usdkrw"].dropna()
            if len(prev):
                fx = float(prev.iloc[-1])
                logs.append(f"환율 수집 실패 → 직전 저장값 {fx:,.2f}원을 사용합니다.")
                for r in rows:
                    r["fx_usdkrw"] = round(fx, 2)

        if not rows:
            st.error("가져온 데이터가 없습니다. 기준일이 휴장일인지 확인하세요.")
        else:
            supabase.table("inv_pf_prices").upsert(
                rows, on_conflict="ticker,price_date").execute()
            _q_prices.clear()

            # --- 전 학생 스냅샷 재계산 ---
            newpdf = price_table()
            pm = price_map_on(newpdf, target)
            snap_rows = []
            for s in students:
                so = student_orders(all_orders, s["name"])
                q, csh = positions_from(so)
                if not q and abs(csh - INITIAL_CAPITAL) < 1e-6 and not so:
                    continue
                _, tot = valuate(q, csh, pm)
                snap_rows.append({
                    "class_name": my_class, "name": s["name"],
                    "price_date": target.isoformat(),
                    "total_value": round(tot, 4), "cash": round(csh, 4)})
            if snap_rows:
                supabase.table("inv_pf_snapshots").upsert(
                    snap_rows, on_conflict="class_name,name,price_date").execute()

            set_status(my_class, last_price_date=target.isoformat())
            clear_all_cache()

            st.success(f"{len(rows)}개 종목 수집 완료 · 학생 {len(snap_rows)}명 평가금액 갱신"
                       + (f" · 환율 {fx:,.2f}원" if fx else ""))
            if missing:
                st.warning("수집 실패: " + ", ".join(missing))
                st.session_state["missing_tickers"] = missing_tk
                st.session_state["missing_date"] = target.isoformat()
            else:
                st.session_state.pop("missing_tickers", None)
            for l in logs:
                st.caption(l)
            st.info("화면 상단의 [현황 새로고침]을 눌러 반영된 결과를 보세요.")

    # ---------------- 수동 보정 (자동 수집 실패 시) ----------------
    miss = st.session_state.get("missing_tickers") or []
    with st.expander(f"✍️ 종가 직접 입력 {'· 보정 필요 ' + str(len(miss)) + '건' if miss else ''}",
                     expanded=bool(miss)):
        st.caption("자동 수집이 실패한 종목은 여기에 직접 넣으면 됩니다. "
                   "네이버·야후에서 해당 날짜 종가를 보고 그대로 입력하세요. "
                   "해외 종목은 **달러 가격 그대로** 넣으면 환율이 자동 적용됩니다.")

        mdate = st.date_input("보정할 기준일", value=datetime.strptime(
            st.session_state.get("missing_date", target.isoformat()),
            "%Y-%m-%d").date(), key="mdate")

        pool = miss if miss else TRADABLE + ["BENCH-KOSPI", "BENCH-SP500"]
        chosen = st.multiselect("입력할 종목", pool, default=miss[:12],
                                format_func=lambda t: SEC_MAP.get(t, {}).get("name", t),
                                key="mtk")

        cur_fx = None
        if not pdf.empty:
            fxs = pdf["fx_usdkrw"].dropna()
            cur_fx = float(fxs.iloc[-1]) if len(fxs) else None
        man_fx = st.number_input("원달러 환율 (해외 종목이 있으면 필수)",
                                 0.0, 5000.0, float(cur_fx or 0.0), step=1.0,
                                 key="mfx")

        vals = {}
        if chosen:
            mc = st.columns(min(len(chosen), 3))
            for i, t in enumerate(chosen):
                unit = "USD" if SEC_MAP.get(t, {}).get("currency") == "USD" else "원"
                vals[t] = mc[i % len(mc)].number_input(
                    f"{SEC_MAP.get(t, {}).get('name', t)} ({unit})",
                    0.0, 1e9, 0.0, step=0.01, format="%.4f", key=f"mv{t}")

        if st.button("💾 입력한 종가 저장", type="primary", disabled=not chosen):
            mrows = [{"ticker": t, "price_date": mdate.isoformat(),
                      "close_price": float(v),
                      "fx_usdkrw": float(man_fx) if man_fx else None}
                     for t, v in vals.items() if v > 0]
            if not mrows:
                st.error("0보다 큰 값을 하나 이상 입력하세요.")
            else:
                supabase.table("inv_pf_prices").upsert(
                    mrows, on_conflict="ticker,price_date").execute()
                _q_prices.clear()

                # 평가금액 다시 계산
                pm2 = price_map_on(price_table(), mdate)
                srows = []
                for s in students:
                    so = student_orders(all_orders, s["name"])
                    if not so:
                        continue
                    q, csh = positions_from(so)
                    _, tot = valuate(q, csh, pm2)
                    srows.append({"class_name": my_class, "name": s["name"],
                                  "price_date": mdate.isoformat(),
                                  "total_value": round(tot, 4), "cash": round(csh, 4)})
                if srows:
                    supabase.table("inv_pf_snapshots").upsert(
                        srows, on_conflict="class_name,name,price_date").execute()
                set_status(my_class, last_price_date=mdate.isoformat())
                st.session_state["missing_tickers"] = [
                    t for t in miss if t not in {r["ticker"] for r in mrows}]
                clear_all_cache()
                st.success(f"{len(mrows)}건 저장 완료 · 평가금액 재계산")
                st.rerun()

    st.write("---")

    # ---------------- 체결 ----------------
    st.subheader("⚙️ 주문 체결")
    pend = [o for o in all_orders if o["status"] == "pending"]
    pend_rounds = sorted({o["round_no"] for o in pend})
    if not pend:
        st.caption("체결 대기 중인 주문이 없습니다.")
    else:
        st.write(f"체결 대기: **{len(pend)}건** (라운드 {pend_rounds})")
        e1, e2 = st.columns([3, 2])
        exec_round = e1.selectbox("체결할 라운드", pend_rounds)
        run = e2.button("⚙️ 이 라운드 체결하기", type="primary", use_container_width=True)

        if run:
            if not pmap_now:
                st.error("기준가가 없습니다. 먼저 기준가 업데이트를 실행하세요.")
            else:
                updates, skipped = [], []
                by_student = {}
                for o in pend:
                    if o["round_no"] == exec_round:
                        by_student.setdefault(o["name"], []).append(o)

                # upsert 는 INSERT ... ON CONFLICT 이므로 NOT NULL 컬럼이 모두
                # 들어 있어야 하고, 모든 행의 키 집합이 같아야 한다.
                def make_row(o, status, px=None, qty=None):
                    return {
                        "id": o["id"],
                        "class_name": o["class_name"],
                        "name": o["name"],
                        "round_no": o["round_no"],
                        "ticker": o["ticker"],
                        "side": o["side"],
                        "amount_manwon": o.get("amount_manwon"),
                        "sell_ratio": o.get("sell_ratio"),
                        "exec_price_krw": round(px, 4) if px else None,
                        "exec_qty": round(qty, 8) if qty else None,
                        "exec_date": last_pd.isoformat() if status == "executed" else None,
                        "status": status,
                    }

                for sname, olist in by_student.items():
                    so = student_orders(all_orders, sname)
                    q, csh = positions_from(so, upto_round=exec_round - 1)

                    # (1) 매도 먼저 — 매도 대금이 매수 재원이 된다
                    for o in [x for x in olist if x["side"] == "sell"]:
                        px = pmap_now.get(o["ticker"])
                        held = q.get(o["ticker"], 0.0)
                        if not px or held <= 0:
                            skipped.append(f"{sname}/{label(o['ticker'])} 매도")
                            updates.append(make_row(o, "cancelled"))
                            continue
                        dq = held * float(o["sell_ratio"])
                        q[o["ticker"]] = held - dq
                        csh += dq * px / 10000
                        updates.append(make_row(o, "executed", px, dq))

                    # (2) 매수 — 현금이 모자라면 비율대로 축소
                    blist = [x for x in olist if x["side"] == "buy"]
                    want = sum(float(x["amount_manwon"] or 0) for x in blist)
                    if want <= 0:
                        scale = 0.0
                    elif want <= csh:
                        scale = 1.0
                    elif csh > 0:
                        scale = csh / want          # 현금 부족 → 비율대로 축소
                    else:
                        scale = 0.0
                    for o in blist:
                        px = pmap_now.get(o["ticker"])
                        amt = float(o["amount_manwon"] or 0) * scale
                        if not px or amt <= 0:
                            skipped.append(f"{sname}/{label(o['ticker'])} 매수")
                            updates.append(make_row(o, "cancelled"))
                            continue
                        eq = amt * 10000 / px
                        csh -= amt
                        updates.append(make_row(o, "executed", px, eq))

                if updates:
                    supabase.table("inv_pf_orders").upsert(
                        updates, on_conflict="id").execute()
                clear_all_cache()
                st.success(f"라운드 {exec_round} 체결 완료 — {len(updates)}건 "
                           f"(기준가 {last_pd})")
                if skipped:
                    st.warning("체결 불가로 취소: " + ", ".join(skipped[:20]))
                st.info("[현황 새로고침]을 누른 뒤 기준가 업데이트를 다시 실행하면 "
                        "평가금액에 반영됩니다.")

    st.write("---")

    # ---------------- 제출 현황 ----------------
    st.subheader("📋 이번 라운드 제출 현황")
    if rnd in TRADING_ROUNDS:
        submitted = {r["name"] for r in refls if r["round_no"] == rnd}
        allnames = {s["name"] for s in students}
        notyet = sorted(allnames - submitted)
        p1, p2, p3 = st.columns(3)
        p1.metric("제출 완료", f"{len(submitted)} 명")
        p2.metric("미제출", f"{len(notyet)} 명")
        nt = sum(1 for r in refls if r["round_no"] == rnd and r.get("no_trade"))
        p3.metric("거래 안 함", f"{nt} 명")
        if notyet:
            st.caption("미제출: " + ", ".join(notyet))
    else:
        st.caption("매매 라운드가 아닙니다.")

    st.write("---")

    # ---------------- 반 전체 현황 ----------------
    st.subheader("📊 반 전체 성과")

    if not snaps:
        st.info("아직 스냅샷이 없습니다. 기준가 업데이트를 실행하면 생성됩니다.")
    else:
        sdf = pd.DataFrame(snaps)
        sdf["price_date"] = pd.to_datetime(sdf["price_date"])
        sdf["total_value"] = sdf["total_value"].astype(float)
        sdf = sdf.sort_values(["name", "price_date"])

        # --- 학생별 지표 ---
        recs = []
        for nm, g in sdf.groupby("name"):
            vals = g["total_value"].to_numpy()
            if len(vals) == 0:
                continue
            ret = (vals[-1] / INITIAL_CAPITAL - 1) * 100
            peak = pd.Series(vals).cummax()
            mdd = float(((pd.Series(vals) - peak) / peak).min() * 100)
            wk = pd.Series(vals).pct_change().dropna()
            vol = float(wk.std() * 100) if len(wk) > 1 else 0.0
            recs.append({"이름": nm, "수익률(%)": round(ret, 2),
                         "최대낙폭(%)": round(mdd, 2),
                         "주간변동성(%)": round(vol, 2),
                         "평가금액": round(vals[-1], 1)})
        perf = pd.DataFrame(recs)

        # --- 벤치마크 ---
        bench_txt = []
        dates = available_dates(pdf)
        if len(dates) >= 2:
            d0, d1 = dates[0], dates[-1]
            for bt, bname in [("BENCH-KOSPI", "코스피"), ("BENCH-SP500", "S&P500")]:
                a = pdf[(pdf.ticker == bt) & (pdf.price_date == d0)]["close_price"]
                b = pdf[(pdf.ticker == bt) & (pdf.price_date == d1)]["close_price"]
                if len(a) and len(b) and float(a.iloc[0]):
                    bench_txt.append(f"{bname} {(float(b.iloc[0])/float(a.iloc[0])-1)*100:+.2f}%")

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("반 평균 수익률", f"{perf['수익률(%)'].mean():+.2f} %")
        k2.metric("최고 / 최저", f"{perf['수익률(%)'].max():+.1f} / "
                                f"{perf['수익률(%)'].min():+.1f} %")
        k3.metric("손실 학생", f"{int((perf['수익률(%)'] < 0).sum())} / {len(perf)} 명")
        k4.metric("벤치마크", " · ".join(bench_txt) if bench_txt else "—")

        tab1, tab2, tab3, tab4 = st.tabs(
            ["익명 산점도", "수익률 분포", "매매 행태", "학생별 표"])

        with tab1:
            st.caption("점 하나가 학생 한 명입니다. 이름은 표시하지 않습니다. "
                       "가로축이 오른쪽일수록(0에 가까울수록) 덜 흔들린 포트폴리오입니다.")
            axis = st.radio("가로축", ["최대낙폭(%)", "주간변동성(%)"], horizontal=True)
            st.scatter_chart(perf, x=axis, y="수익률(%)", height=420)
            st.caption("같은 수익률이라도 가로 위치가 다르다는 점을 보여주세요. "
                       "2주차 변동성 개념이 여기서 자기 점의 좌표가 됩니다.")

        with tab2:
            hist = perf["수익률(%)"].round(0).value_counts().sort_index()
            st.bar_chart(hist, height=320)
            st.caption("이름 없이 분포만 보여주는 화면입니다. 순위표 대신 이 그림을 쓰세요.")

        with tab3:
            cnt = {}
            for o in all_orders:
                if o["status"] != "executed":
                    continue
                cnt[o["name"]] = cnt.get(o["name"], 0) + 1
            if cnt:
                tdf = pd.DataFrame({"매매횟수": list(cnt.values())})
                st.bar_chart(tdf["매매횟수"].value_counts().sort_index(), height=260)
                merged = perf.copy()
                merged["매매횟수"] = merged["이름"].map(cnt).fillna(0)
                lo = merged[merged["매매횟수"] <= merged["매매횟수"].median()]
                hi = merged[merged["매매횟수"] > merged["매매횟수"].median()]
                cc1, cc2 = st.columns(2)
                cc1.metric("적게 매매한 절반 평균", f"{lo['수익률(%)'].mean():+.2f} %")
                cc2.metric("많이 매매한 절반 평균", f"{hi['수익률(%)'].mean():+.2f} %")
                st.caption("11주차 행동재무 수업에서 이 두 숫자를 나란히 보여주세요.")

            st.write("**가장 많이 매매된 종목**")
            tk = {}
            for o in all_orders:
                if o["status"] != "executed":
                    continue
                key = label(o["ticker"])
                tk.setdefault(key, {"매수": 0, "매도": 0})
                tk[key]["매수" if o["side"] == "buy" else "매도"] += 1
            if tk:
                tkdf = pd.DataFrame(tk).T
                tkdf["합계"] = tkdf.sum(axis=1)
                st.dataframe(tkdf.sort_values("합계", ascending=False).head(12),
                             use_container_width=True)

        with tab4:
            bit_map = {s["name"]: s.get("bit_type") for s in students}
            perf2 = perf.copy()
            perf2["BIT유형"] = perf2["이름"].map(bit_map)
            exec_cnt = {}
            for o in all_orders:
                if o["status"] == "executed":
                    exec_cnt[o["name"]] = exec_cnt.get(o["name"], 0) + 1
            perf2["매매횟수"] = perf2["이름"].map(exec_cnt).fillna(0).astype(int)
            st.dataframe(perf2.sort_values("수익률(%)", ascending=False),
                         use_container_width=True, hide_index=True)
            st.caption("이 표는 교수 화면에만 나타납니다. 학생 화면에는 순위가 없습니다.")

    # ---------------- 라운드별 기록 열람 ----------------
    st.write("---")
    with st.expander("💬 라운드별 학생 기록 (심정 · 이유)"):
        rsel = st.selectbox("라운드 선택", TRADING_ROUNDS, key="rsel")
        anon = st.checkbox("이름 가리고 보기 (스크린 공유용)", value=True)
        target_r = [r for r in refls if r["round_no"] == rsel]
        if not target_r:
            st.caption("기록이 없습니다.")
        for i, r in enumerate(sorted(target_r, key=lambda x: x["name"]), 1):
            who = f"학생 {i}" if anon else r["name"]
            tag = " · 거래 없음" if r.get("no_trade") else ""
            st.markdown(f"**{who}**{tag}")
            st.caption(f"심정 — {r.get('feeling') or '—'}")
            st.caption(f"이유 — {r.get('reason') or '—'}")

    # ---------------- 데이터 관리 ----------------
    st.write("---")
    with st.expander("⚠️ 데이터 관리"):
        st.caption("전체 데이터를 CSV로 내려받아 두시면 안전합니다.")
        exp = io.StringIO()
        exp.write("# orders\n")
        exp.write(pd.DataFrame(all_orders).to_csv(index=False) if all_orders else "\n")
        exp.write("\n# reflections\n")
        exp.write(pd.DataFrame(refls).to_csv(index=False) if refls else "\n")
        exp.write("\n# snapshots\n")
        exp.write(pd.DataFrame(snaps).to_csv(index=False) if snaps else "\n")
        st.download_button("📥 이 분반 전체 데이터 내려받기",
                           exp.getvalue().encode("utf-8-sig"),
                           file_name=f"{my_class}_전체데이터.csv", mime="text/csv")

        st.write("")
        confirm = st.text_input("초기화하려면 분반 이름을 그대로 입력하세요", key="delconf")
        if st.button("이 분반 전체 초기화") and confirm == my_class:
            for tbl in ("inv_pf_orders", "inv_pf_reflections",
                        "inv_pf_snapshots", "inv_pf_students"):
                supabase.table(tbl).delete().eq("class_name", my_class).execute()
            set_status(my_class, current_round=0, phase="대기", last_price_date=None)
            clear_all_cache()
            st.rerun()
