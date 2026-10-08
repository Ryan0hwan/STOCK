"""
MA 터치 스캐너
- S&P 500 / Nasdaq 100 / Russell 2000(대형·고유동성만) / 내 관심종목을 훑어서
- 당일 가격 범위가 50일 SMA, 100일 VWMA, 200일 SMA, 325일 SMA에 닿은 종목을
- 디스코드 채널로 표 형태로 보냅니다.
"""
import datetime as dt
import re
import io
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ───────────── 설정 (여기만 고치면 됩니다) ─────────────
GROUPS = os.environ.get("SCAN_GROUPS", "sp500,watchlist").split(",")  # 앞 그룹 우선 (겹치는 종목은 앞 그룹에만 표시)
TOL_PCT = float(os.environ.get("TOL_PCT", "0.5"))      # 터치 허용 오차(%). 선의 ±0.5% 안에 들어오면 터치로 봄
ONLY_FROM_ABOVE = os.environ.get("ONLY_FROM_ABOVE", "0") == "1"  # 1이면 전일 종가가 선 위에 있던 종목만 (눌림목). 0이면 양방향 모두
REQUIRE_GOLDEN = os.environ.get("REQUIRE_GOLDEN", "1") == "1"   # 1이면 정배열(50일 SMA > 200일 SMA) 종목만 알림
REQUIRE_CLOSE_ABOVE = os.environ.get("REQUIRE_CLOSE_ABOVE", "1") == "1"  # 1이면 종가가 터치한 선 위에서 마감한 경우만
FIRST_TOUCH_DAYS = int(os.environ.get("FIRST_TOUCH_DAYS", "20"))  # 위에서 내려온 터치는 최근 N일간 종가가 그 선 아래로 간 적 없어야 함 (0이면 해제)
REQUIRE_SLOPE = os.environ.get("REQUIRE_SLOPE", "1") == "1"      # 1이면 50일선(20일 전 대비)·200일선(1개월 전 대비)이 모두 상승 중인 종목만
REQUIRE_RS = os.environ.get("REQUIRE_RS", "1") == "1"            # 1이면 최근 3개월·6개월 수익률이 모두 S&P 500(SPY)보다 높은 종목만
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5"))    # 이 가격 미만 종목 제외
MIN_DOLLAR_VOL = float(os.environ.get("MIN_DOLLAR_VOL", "5000000"))  # 50일 평균 거래대금($) 하한 (전체 공통)
RUSSELL_MIN_DOLLAR_VOL = float(os.environ.get("RUSSELL_MIN_DOLLAR_VOL", "50000000"))  # Russell 2000 종목: 50일 평균 거래대금 $50M 이상만
RUSSELL_MIN_MCAP = float(os.environ.get("RUSSELL_MIN_MCAP", "2000000000"))            # Russell 2000 종목: 시가총액 $2B 이상만
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
FORCE = os.environ.get("FORCE", "0") == "1"            # 장 시간이 아니어도 강제 실행
# ──────────────────────────────────────────────────────

NY = ZoneInfo("America/New_York")
STATE = Path(__file__).parent / "state"
STATE.mkdir(exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (ma-touch-scanner)"}
GROUP_LABEL = {"sp500": "S&P 500", "nasdaq100": "Nasdaq 100", "russell2000": "Russell 2000", "watchlist": "Watchlist"}
MA_NAMES = ["50D SMA", "100D VWMA", "200D SMA", "325D SMA"]
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


# ───────────── 종목 목록 ─────────────
def _fetch_sp500() -> pd.DataFrame:
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=UA, timeout=30).text
    t = pd.read_html(io.StringIO(html))[0]
    return pd.DataFrame({"ticker": t["Symbol"], "name": t["Security"], "industry": t["GICS Sub-Industry"]})


def _fetch_nasdaq100() -> pd.DataFrame:
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    html = requests.get(url, headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        cols = {str(c).strip().lower(): c for c in t.columns}
        tk = cols.get("ticker") or cols.get("symbol")
        if tk is not None and 90 <= len(t) <= 110:
            name = cols.get("company") or cols.get("security") or tk
            ind = cols.get("gics sub-industry") or cols.get("gics sector") or cols.get("industry")
            return pd.DataFrame({"ticker": t[tk], "name": t[name],
                                 "industry": t[ind] if ind is not None else ""})
    raise ValueError("Nasdaq-100 구성종목 표를 찾지 못함")


def _fetch_russell2000() -> pd.DataFrame:
    url = ("https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
           "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund")
    r = requests.get(url, headers=BROWSER_UA, timeout=60)
    r.raise_for_status()
    lines = r.content.decode("utf-8-sig", errors="replace").splitlines()
    # 앞쪽 펀드 설명, 뒤쪽 면책 문구를 건너뛰고 종목 표만 읽음
    start = next(i for i, ln in enumerate(lines) if ln.replace('"', "").startswith("Ticker,"))
    t = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip", dtype=str)
    t.columns = [c.strip() for c in t.columns]
    t = t[t["Asset Class"].astype(str).str.strip() == "Equity"]
    return pd.DataFrame({"ticker": t["Ticker"], "name": t["Name"], "industry": t["Sector"]})


def _load_watchlist() -> pd.DataFrame:
    p = Path(__file__).parent / "watchlist.txt"
    rows = []
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.split("#")[0].strip()
            if not ln:
                continue
            tk, _, name = ln.partition(",")          # "티커, 회사명" 형식 (회사명은 생략 가능)
            tk = re.sub(r"\.([A-Z])$", r"-\1", tk.strip().upper())  # MOG.A → MOG-A (야후 표기)
            rows.append({"ticker": tk, "name": name.strip(), "industry": "Watchlist"})
    return pd.DataFrame(rows, columns=["ticker", "name", "industry"]).drop_duplicates("ticker")


def load_universe(group: str) -> pd.DataFrame:
    if group == "watchlist":
        return _load_watchlist()
    cache = STATE / f"universe_{group}.csv"
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 86400
    if not fresh:
        try:
            df = {"sp500": _fetch_sp500, "nasdaq100": _fetch_nasdaq100, "russell2000": _fetch_russell2000}[group]()
            if len(df) < 50:
                raise ValueError("종목 수가 비정상적으로 적음")
            df.to_csv(cache, index=False)
        except Exception as e:  # 실패하면 지난 목록을 그대로 사용
            print(f"[경고] {group} 목록 갱신 실패: {e}")
    if not cache.exists():
        return pd.DataFrame(columns=["ticker", "name", "industry"])
    df = pd.read_csv(cache).fillna("")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.replace(".", "-", regex=False)  # BRK.B → BRK-B
    df = df[df["ticker"].str.fullmatch(r"[A-Z\-]{1,6}")]
    return df.drop_duplicates("ticker")


# ───────────── 시세 ─────────────
def download(tickers: list[str]):
    import yfinance as yf
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            data = yf.download(chunk, period="2y", interval="1d", group_by="ticker",
                               auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            print(f"[경고] 시세 다운로드 실패({chunk[0]}…): {e}")
            continue
        if not isinstance(data.columns, pd.MultiIndex):
            data = pd.concat({chunk[0]: data}, axis=1)
        have = set(data.columns.get_level_values(0))
        for t in chunk:
            if t in have:
                yield t, data[t]
        time.sleep(1)


def check_touches(df: pd.DataFrame, today: dt.date | None, spy_ret: dict | None = None,
                  min_dollar_vol: float = MIN_DOLLAR_VOL) -> list[dict]:
    """한 종목의 일봉에서 오늘 조건을 통과한 터치를 반환 (today가 None이면 가장 최근 거래일 기준)"""
    df = df.dropna(subset=["Close", "High", "Low", "Volume"])
    if len(df) < 60 or (today is not None and df.index[-1].date() != today):
        return []
    c, v = df["Close"], df["Volume"]
    last, prev = df.iloc[-1], df.iloc[-2]
    if last["Close"] < MIN_PRICE or (c * v).tail(50).mean() < max(MIN_DOLLAR_VOL, min_dollar_vol):
        return []
    series = {
        "50D SMA": c.rolling(50).mean(),
        "100D VWMA": (c * v).rolling(100).sum() / v.rolling(100).sum(),
        "200D SMA": c.rolling(200).mean(),
        "325D SMA": c.rolling(325).mean(),   # 상장 325거래일 미만이면 이 선만 건너뜀
    }
    mas = {k: s.iloc[-1] for k, s in series.items()}
    s50, s200 = series["50D SMA"], series["200D SMA"]

    # 정배열: 50일선 > 200일선 (200일선이 없으면 제외)
    if REQUIRE_GOLDEN and (pd.isna(mas["50D SMA"]) or pd.isna(mas["200D SMA"]) or not mas["50D SMA"] > mas["200D SMA"]):
        return []

    # 이평선 기울기: 50일선은 20거래일 전보다, 200일선은 21거래일(약 1개월) 전보다 높아야 함
    if REQUIRE_SLOPE:
        if len(df) < 222:
            return []
        a50, b50, a200, b200 = s50.iloc[-1], s50.iloc[-21], s200.iloc[-1], s200.iloc[-22]
        if pd.isna(b50) or pd.isna(b200) or not (a50 > b50 and a200 > b200):
            return []

    # 상대강도: 3개월(63일)·6개월(126일) 수익률이 모두 SPY보다 높아야 함
    if REQUIRE_RS and spy_ret:
        if len(c) < 127:
            return []
        r3, r6 = c.iloc[-1] / c.iloc[-64] - 1, c.iloc[-1] / c.iloc[-127] - 1
        if not (r3 > spy_ret["3m"] and r6 > spy_ret["6m"]):
            return []

    tol = TOL_PCT / 100
    out = []
    for name, ma in mas.items():
        if pd.isna(ma):
            continue
        touched = last["Low"] <= ma * (1 + tol) and last["High"] >= ma * (1 - tol)
        if not touched:
            continue
        from_above = prev["Close"] > ma
        if ONLY_FROM_ABOVE and not from_above:
            continue
        # 선 위에서 마감
        if REQUIRE_CLOSE_ABOVE and not last["Close"] > ma:
            continue
        # 첫 번째 터치: 위에서 내려온 경우, 오늘 이전 N일 동안 종가가 그 선 아래로 간 적 없어야 함
        # (아래에서 올라온 '회복' 터치에는 적용하지 않음 — 정의상 전일 종가가 선 아래이기 때문)
        if FIRST_TOUCH_DAYS > 0 and from_above:
            past_c = c.iloc[-1 - FIRST_TOUCH_DAYS:-1]
            past_ma = series[name].iloc[-1 - FIRST_TOUCH_DAYS:-1]
            if past_ma.isna().any() or (past_c <= past_ma).any():
                continue
        out.append({"ma_name": name, "ma": float(ma), "low": float(last["Low"]), "last": float(last["Close"])})
    return out


def market_cap(ticker: str, price: float) -> float | None:
    """상장주식수 × 현재가. 주식수는 7일간 캐시. 조회 실패 시 None"""
    cache_file = STATE / "shares.json"
    try:
        cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    except Exception:
        cache = {}
    hit = cache.get(ticker)
    if hit and time.time() - hit["ts"] < 7 * 86400:
        return hit["shares"] * price
    try:
        import yfinance as yf
        fi = yf.Ticker(ticker).fast_info
        shares = fi.get("shares") if hasattr(fi, "get") else getattr(fi, "shares", None)
        if not shares:
            mc = fi.get("marketCap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
            return float(mc) if mc else None
        cache[ticker] = {"shares": float(shares), "ts": time.time()}
        cache_file.write_text(json.dumps(cache))
        return float(shares) * price
    except Exception as e:
        print(f"[경고] {ticker} 시가총액 조회 실패: {e}")
        return None


# ───────────── 디스코드 ─────────────
def post(content: str):
    if not WEBHOOK:
        print(content)
        return
    for _ in range(5):
        r = requests.post(WEBHOOK, json={"content": content}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        r.raise_for_status()
        time.sleep(0.6)
        return


def send_table(title: str, rows: list[dict]):
    head = f"{'TIME':<6}{'TICKER':<8}{'DESCRIPTION':<22}{'INDUSTRY':<22}{'MA':>9}{'LOW':>9}{'LAST':>9}"
    lines = [
        f"{r['time']:<6}{r['ticker']:<8}{r['name'][:20]:<22}{r['industry'][:20]:<22}"
        f"{r['ma']:>9.2f}{r['low']:>9.2f}{r['last']:>9.2f}"
        for r in rows
    ]
    first = True
    while lines:
        block, size = [], 0
        while lines and size + len(lines[0]) + 1 < 1700:
            size += len(lines[0]) + 1
            block.append(lines.pop(0))
        header = f"**{title}**\n" if first else ""
        post(f"{header}```\n{head}\n" + "\n".join(block) + "\n```")
        first = False


# ───────────── 실행 ─────────────
def main():
    now = dt.datetime.now(NY)
    is_open = now.weekday() < 5 and dt.time(9, 35) <= now.time() <= dt.time(17, 30)  # 장 마감 직후 실행분까지 포함
    if not is_open and not FORCE:
        print(f"장 시간이 아님 ({now:%Y-%m-%d %H:%M} ET). 종료.")
        return
    today = now.date()

    # 오늘 이미 보낸 알림 (같은 종목·같은 선은 하루 1회)
    sent_file = STATE / f"sent_{today}.json"
    sent = set(json.loads(sent_file.read_text())) if sent_file.exists() and not FORCE else set()
    for old in STATE.glob("sent_*.json"):
        if old != sent_file:
            old.unlink()

    universes = {g.strip(): load_universe(g.strip()) for g in GROUPS if g.strip()}
    meta, group_of = {}, {}
    for g, u in universes.items():
        for row in u.itertuples():
            if row.ticker not in meta:          # 여러 그룹에 있으면 먼저 나온 그룹으로 분류
                meta[row.ticker] = row
                group_of[row.ticker] = g
    print(f"스캔 대상 {len(meta)}종목 " + ", ".join(f"{g}={len(u)}" for g, u in universes.items()))
    failed = [GROUP_LABEL.get(g, g) for g, u in universes.items() if g != "watchlist" and len(u) == 0]
    if failed and meta:
        post(f"⚠️ MA 스캐너: {', '.join(failed)} 종목 목록을 불러오지 못해 이번 실행에서 빠졌습니다.")
    if not meta:
        post("⚠️ MA 스캐너: 종목 목록을 불러오지 못했습니다. Actions 로그를 확인하세요.")
        return

    # 상대강도 비교용 SPY 수익률
    spy_ret = None
    if REQUIRE_RS:
        for _, sdf in download(["SPY"]):
            sc = sdf["Close"].dropna()
            if len(sc) >= 127:
                spy_ret = {"3m": sc.iloc[-1] / sc.iloc[-64] - 1, "6m": sc.iloc[-1] / sc.iloc[-127] - 1}
        if spy_ret is None:
            print("[경고] SPY 시세를 받지 못해 이번 실행은 상대강도 조건을 건너뜁니다.")

    found: dict[tuple, list] = {}
    scanned = 0
    for ticker, df in download(list(meta)):
        scanned += 1
        is_russell = group_of[ticker] == "russell2000"
        touches = check_touches(df, None if FORCE else today, spy_ret,
                                RUSSELL_MIN_DOLLAR_VOL if is_russell else MIN_DOLLAR_VOL)
        if touches and is_russell:  # Russell 종목은 알림 후보일 때만 시가총액 확인 (조회 실패 시 통과)
            mc = market_cap(ticker, touches[0]["last"])
            if mc is not None and mc < RUSSELL_MIN_MCAP:
                touches = []
        for t in touches:  # 테스트 실행은 최근 거래일 기준
            key = f"{ticker}|{t['ma_name']}"
            if key in sent:
                continue
            sent.add(key)
            m = meta[ticker]
            found.setdefault((group_of[ticker], t["ma_name"]), []).append(
                {"time": f"{now:%H:%M}", "ticker": ticker, "name": str(m.name), "industry": str(m.industry), **t})

    total = sum(len(v) for v in found.values())
    print(f"시세 확인 {scanned}종목, 신규 터치 {total}건")
    for g in universes:
        for ma_name in MA_NAMES:
            rows = found.get((g, ma_name))
            if rows:
                rows.sort(key=lambda r: r["ticker"])
                send_table(f"{now:%b} {now.day} | Intraday Touch of {ma_name} | {GROUP_LABEL.get(g, g)}", rows)

    if scanned < len(meta) * 0.5:
        post(f"⚠️ MA 스캐너: 시세를 {scanned}/{len(meta)}종목만 받았습니다. 일시적 오류일 수 있습니다.")
    if FORCE:
        post(f"✅ MA 스캐너 테스트 완료: {scanned}종목 확인, 터치 {total}건 (최근 거래일 기준)")
    else:
        sent_file.write_text(json.dumps(sorted(sent)))


if __name__ == "__main__":
    sys.exit(main())
