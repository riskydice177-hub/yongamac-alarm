#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yongamac_alarm.py  (v2 - JSON API)
CGV 용산아이파크몰 IMAX(용아맥) 예매 오픈 감지 -> 텔레그램 개인 알림

개인용 모니터링 도구입니다. 공개 API만 조회하며, 자동 예매(매크로) 기능은
의도적으로 넣지 않았습니다. 폴링 간격을 지켜주세요.

사용:
    TG_BOT_TOKEN / TG_CHAT_ID 환경변수 설정 후

    python yongamac_alarm.py --probe     # API/파싱 점검
    python yongamac_alarm.py --test      # 텔레그램 전송 테스트
    python yongamac_alarm.py             # 감시 시작 (상시 루프)
    python yongamac_alarm.py --once      # 1회 검사 (cron / GitHub Actions)
    python yongamac_alarm.py --reset     # 저장 상태 초기화
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

CO_CD = os.environ.get("CGV_CO_CD", "A420")
SITE_NO = os.environ.get("CGV_SITE_NO", "0013")          # 0013 = 용산아이파크몰
THEATER_NAME = os.environ.get("CGV_THEATER_NAME", "용아맥")

# 감시할 포맷. "4DX", "SCREENX", "돌비" 등으로 바꾸면 그대로 재사용 가능.
FORMAT_KEYWORDS = [k.strip() for k in
                   os.environ.get("CGV_FORMATS", "IMAX,아이맥스").split(",") if k.strip()]

# 특정 영화만 보고 싶으면 지정 (비우면 전체)
TITLE_FILTER = [k.strip() for k in
                os.environ.get("CGV_TITLES", "").split(",") if k.strip()]

DAYS_AHEAD = int(os.environ.get("CGV_DAYS_AHEAD", "30"))
POLL_SECONDS = int(os.environ.get("CGV_POLL_SECONDS", "25"))   # 20 미만 비권장
HOT_PER_CYCLE = int(os.environ.get("CGV_HOT_PER_CYCLE", "8"))  # 사이클당 미오픈 날짜 조회 수
COLD_EVERY = int(os.environ.get("CGV_COLD_EVERY", "8"))        # 이미 열린 날짜 재확인 주기

HEARTBEAT_HOURS = int(os.environ.get("CGV_HEARTBEAT_HOURS", "1"))  # 0 이면 끔

TIMEOUT = 12
STATE_PATH = os.environ.get("CGV_STATE_PATH", "yongamac_state.json")

API_URL = ("https://cgv.co.kr/api/v1/booking/searchMovScnInfo"
           "?coCd={co}&siteNo={site}&scnYmd={ymd}&rtctlScopCd=08")
BOOKING_URL = os.environ.get("CGV_BOOKING_URL", "https://cgv.co.kr/")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%m-%d %H:%M:%S}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
# HTTP — CGV는 봇 판정이 있어서 브라우저처럼 접근해야 한다
# ─────────────────────────────────────────────────────────────

_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_WARMED = False

API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://cgv.co.kr/",
    "Origin": "https://cgv.co.kr",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "sec-ch-ua": '"Chromium";v="126", "Not(A:Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

PAGE_HEADERS = dict(API_HEADERS, **{
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Upgrade-Insecure-Requests": "1",
})


def _decode(resp) -> str:
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    cs = resp.headers.get_content_charset()
    for c in filter(None, [cs, "utf-8", "euc-kr", "cp949"]):
        try:
            return raw.decode(c)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _warmup() -> None:
    """크롬처럼 메인 페이지를 먼저 들러 세션 쿠키를 받는다."""
    global _WARMED
    if _WARMED:
        return
    try:
        req = urllib.request.Request("https://cgv.co.kr/", headers=PAGE_HEADERS)
        with _OPENER.open(req, timeout=TIMEOUT) as r:
            r.read(4096)
        time.sleep(0.6)
    except Exception as e:
        log(f"워밍업 실패(무시): {type(e).__name__}")
    _WARMED = True


def http_get(url: str, timeout: int = TIMEOUT) -> str:
    global _WARMED
    _warmup()
    try:
        with _OPENER.open(urllib.request.Request(url, headers=API_HEADERS),
                          timeout=timeout) as resp:
            return _decode(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):        # 쿠키 만료 가능 -> 세션 새로 잡고 1회 재시도
            _WARMED = False
            _JAR.clear()
            _warmup()
            with _OPENER.open(urllib.request.Request(url, headers=API_HEADERS),
                              timeout=timeout) as resp:
                return _decode(resp)
        raise


def tg_api(method: str, params: dict) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN 이 비어 있습니다.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def notify(text: str, silent: bool = False) -> bool:
    if not CHAT_ID:
        log("!! TG_CHAT_ID 없음 — 콘솔 출력만 합니다.\n" + text)
        return False
    for i in range(3):
        try:
            r = tg_api("sendMessage", {
                "chat_id": CHAT_ID, "text": text,
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            })
            if r.get("ok"):
                return True
            log(f"!! 텔레그램 응답: {r}")
        except Exception as e:
            log(f"!! 텔레그램 오류({i+1}/3): {e}")
        time.sleep(1.5 * (i + 1))
    return False


# ─────────────────────────────────────────────────────────────
# 파싱 — JSON 응답에서 대상 지점 + 대상 포맷 회차만 뽑는다
# ─────────────────────────────────────────────────────────────

def fmt_time(v: str) -> str:
    """'1300' -> '13:00'. CGV는 심야를 2500(=새벽 1시)처럼 표기하며 그대로 둔다."""
    s = (v or "").strip().zfill(4)
    return f"{s[:2]}:{s[2:]}" if s.isdigit() else (v or "?")


def to_int(v, default=0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def analyze(body: str) -> dict:
    """반환: {"ok":bool, "shows":[...], "raw_count":int, "note":str}"""
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "shows": [], "raw_count": 0, "note": "JSON 아님"}

    if not isinstance(doc, dict) or "data" not in doc:
        return {"ok": False, "shows": [], "raw_count": 0, "note": "data 필드 없음"}

    rows = doc.get("data") or []
    shows = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # 같은 응답에 씨네드쉐프(P013) 등이 섞여 오므로 지점을 먼저 거른다
        if str(r.get("siteNo", "")).strip() != SITE_NO:
            continue

        hay = " ".join(str(r.get(k) or "") for k in
                       ("scnsEnm", "scnsNm", "expoScnsNm", "tcscnsGradNm",
                        "movkndDsplNm", "expoProdNm"))
        if not any(k.lower() in hay.lower() for k in FORMAT_KEYWORDS):
            continue

        title = (r.get("movNm") or r.get("prodNm") or r.get("expoProdNm") or "?").strip()
        if TITLE_FILTER and not any(f.lower() in title.lower() for f in TITLE_FILTER):
            continue

        shows.append({
            "t": fmt_time(r.get("scnsrtTm")),
            "title": title,
            "hall": (r.get("expoScnsNm") or r.get("scnsNm") or "").strip(),
            "fmt": (r.get("movkndDsplNm") or "").strip(),
            "free": to_int(r.get("frSeatCnt"), -1),
            "total": to_int(r.get("stcnt"), -1),
        })

    shows.sort(key=lambda s: (s["t"], s["title"]))
    return {"ok": True, "shows": shows, "raw_count": len(rows),
            "note": str(doc.get("statusMessage", ""))}


def show_key(s: dict) -> str:
    """회차 식별자. 잔여좌석은 계속 변하므로 키에서 제외한다."""
    return f"{s['hall']}|{s['t']}|{s['title']}"


# ─────────────────────────────────────────────────────────────
# 상태
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def target_dates() -> list:
    today = datetime.now(KST).date()
    return [(today + timedelta(days=d)).strftime("%Y%m%d")
            for d in range(DAYS_AHEAD + 1)]


def _hour_gap(a: str, b: str) -> int:
    """'YYYYMMDDHH' 두 개의 시간 차이(시간 단위)."""
    try:
        fa = datetime.strptime(a, "%Y%m%d%H")
        fb = datetime.strptime(b, "%Y%m%d%H")
        return int(abs((fb - fa).total_seconds()) // 3600)
    except ValueError:
        return 99


def pretty(ymd: str) -> str:
    d = datetime.strptime(ymd, "%Y%m%d").date()
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


# ─────────────────────────────────────────────────────────────
# 검사
# ─────────────────────────────────────────────────────────────

def fetch_day(ymd: str) -> dict:
    return analyze(http_get(API_URL.format(co=CO_CD, site=SITE_NO, ymd=ymd)))


def check_date(ymd: str, st: dict) -> tuple:
    info = fetch_day(ymd)
    if not info["ok"]:
        raise RuntimeError(f"응답 이상: {info['note']}")

    keys = [show_key(s) for s in info["shows"]]
    prev = set(st.get(ymd, {}).get("keys", []))
    new = [s for s in info["shows"] if show_key(s) not in prev]

    st[ymd] = {"keys": keys, "n": len(keys),
               "seen": datetime.now(KST).isoformat(timespec="seconds")}

    # 회차 감소(매진/시간 경과)는 알리지 않는다
    if not new or not st.get("_initialized"):
        return False, ""

    titles = sorted({s["title"] for s in new})
    lines = [f"🚨 예매 오픈! {pretty(ymd)}", " / ".join(titles[:3])]
    for s in new[:8]:
        seat = f" ({s['free']}/{s['total']}석)" if s["total"] > 0 else ""
        lines.append(f"  {s['t']}  {s['hall']}{seat}")
    if len(new) > 8:
        lines.append(f"  … 외 {len(new) - 8}회차")
    lines.append(BOOKING_URL)
    return True, "\n".join(lines)


def run_cycle(st: dict, cycle: int) -> None:
    dates = target_dates()
    hot = [d for d in dates if st.get(d, {}).get("n", 0) == 0]
    cold = [d for d in dates if d not in hot]

    # 미오픈 날짜(프론티어)를 우선 조회. 새 예매는 거의 항상 여기서 열린다.
    if hot:
        start = (cycle * HOT_PER_CYCLE) % len(hot)
        picks = hot[start:start + HOT_PER_CYCLE] or hot[:HOT_PER_CYCLE]
    else:
        picks = []
    if cold and cycle % COLD_EVERY == 0:
        picks = list(picks) + [cold[(cycle // COLD_EVERY) % len(cold)]]

    for ymd in picks:
        hit, msg = check_date(ymd, st)
        if hit:
            log("** ALERT\n" + msg)
            notify(msg)
        time.sleep(random.uniform(0.4, 0.9))


def summary(st: dict, header: str, cycles: int = 0, started: float = 0.0) -> str:
    """하트비트 본문. 지금 무엇을 어디까지 보고 있는지 한눈에."""
    dates = target_dates()
    opened = [d for d in dates if st.get(d, {}).get("n", 0) > 0]
    total = sum(st.get(d, {}).get("n", 0) for d in dates)
    frontier = [d for d in dates if st.get(d, {}).get("n", 0) == 0]

    lines = [header]
    if opened:
        lines.append(f"예매 열린 마지막 날: {pretty(opened[-1])}")
        lines.append(f"감시 중 회차: {total}개 ({len(opened)}일)")
    else:
        lines.append("현재 열린 회차 없음")
    if frontier:
        lines.append(f"다음 오픈 대기: {pretty(frontier[0])} 이후")
    lines.append(f"감시 대상: {', '.join(FORMAT_KEYWORDS)} / {THEATER_NAME}")
    if started:
        up = int(time.time() - started)
        lines.append(f"가동 {up // 3600}시간 {up % 3600 // 60}분 · 조회 {cycles}회")
    return "\n".join(lines)


def watch(once: bool = False) -> None:
    st = load_state()
    if not st.get("_initialized"):
        log(f"최초 실행 — 기준선 수집 ({DAYS_AHEAD + 1}일치, 알림 없음)")
        total = 0
        for ymd in target_dates():
            try:
                info = fetch_day(ymd)
                keys = [show_key(s) for s in info["shows"]]
                st[ymd] = {"keys": keys, "n": len(keys)}
                total += len(keys)
                if keys:
                    names = ", ".join(sorted({s["title"] for s in info["shows"]}))
                    log(f"  {pretty(ymd)}  {len(keys):2d}회차  {names[:40]}")
            except Exception as e:
                log(f"  {ymd} 실패: {type(e).__name__}: {e}")
            time.sleep(0.5)
        st["_initialized"] = True
        save_state(st)
        log(f"기준선 완료 — 총 {total}회차")

    # 시작할 때마다 현황 보고 (재시작 포함)
    started = time.time()
    cur_hour = datetime.now(KST).strftime("%Y%m%d%H")
    st["_last_hb"] = cur_hour
    msg = summary(st, f"✅ {THEATER_NAME} 알리미 가동 시작")
    log(msg)
    if not once:
        notify(msg, silent=True)
    save_state(st)

    cycle = int(time.time() // 300) if once else 0
    fails = 0
    while True:
        try:
            run_cycle(st, cycle)
            if fails >= 3:
                notify("🟢 알리미 복구됨", silent=True)
            fails = 0

            # 매시 정각 하트비트 — 살아있다는 증거
            now_hour = datetime.now(KST).strftime("%Y%m%d%H")
            if HEARTBEAT_HOURS > 0 and now_hour != st.get("_last_hb"):
                last = st.get("_last_hb") or ""
                gap = 99 if not last else _hour_gap(last, now_hour)
                if gap >= HEARTBEAT_HOURS:
                    st["_last_hb"] = now_hour
                    hb = summary(st, "🟢 정상 작동 중", cycle, started)
                    log(hb)
                    notify(hb, silent=True)
            save_state(st)
        except KeyboardInterrupt:
            save_state(st)
            log("종료")
            return
        except Exception as e:
            fails += 1
            log(f"오류({fails}): {type(e).__name__}: {e}")
            if fails == 3:
                notify("⚠️ 알리미가 CGV API를 계속 못 읽고 있습니다. "
                       "--probe 로 확인이 필요합니다.")
            time.sleep(min(300, 20 * (2 ** min(fails, 4))))
            continue

        cycle += 1
        if once:
            save_state(st)
            return
        time.sleep(max(5, POLL_SECONDS + random.uniform(-3, 3)))


# ─────────────────────────────────────────────────────────────
# 보조 모드
# ─────────────────────────────────────────────────────────────

def do_probe(ymd) -> None:
    ymd = ymd or datetime.now(KST).strftime("%Y%m%d")
    url = API_URL.format(co=CO_CD, site=SITE_NO, ymd=ymd)
    print(f"GET {url}\n")
    try:
        body = http_get(url)
    except Exception as e:
        print(f"실패: {type(e).__name__}: {e}")
        print("→ API 주소가 바뀌었을 수 있습니다. 크롬 개발자도구 Network 탭에서")
        print("  searchMovScnInfo 요청을 다시 찾아 API_URL 을 교체하세요.")
        return

    info = analyze(body)
    print(f"응답 {len(body):,}자 / 전체 {info['raw_count']}건 / "
          f"지점{SITE_NO}+{'|'.join(FORMAT_KEYWORDS)} 해당 {len(info['shows'])}건")
    print(f"상태: {info['note']}\n")
    if not info["shows"]:
        print("해당 회차 없음 (아직 예매 미오픈이면 정상입니다)")
        return
    for s in info["shows"]:
        seat = f"{s['free']:>4}/{s['total']:<4}석" if s["total"] > 0 else ""
        print(f"  {s['t']}  {s['hall']:<10} {seat}  {s['fmt']:<18} {s['title']}")


def do_setup() -> None:
    print("1) 텔레그램 @BotFather -> /newbot -> 토큰 복사")
    print("2) 만든 봇에게 아무 메시지나 보내기")
    print("3) TG_BOT_TOKEN 지정 후 이 명령 재실행\n")
    if not BOT_TOKEN:
        print("TG_BOT_TOKEN 이 비어 있습니다.")
        return
    r = tg_api("getUpdates", {})
    seen = {}
    for u in r.get("result", []):
        c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if c.get("id"):
            seen[c["id"]] = c.get("title") or c.get("first_name", "")
    if not seen:
        print("받은 메시지가 없습니다. 봇에게 먼저 말을 걸어주세요.")
    for cid, name in seen.items():
        print(f"  TG_CHAT_ID={cid}   ({name})")


def main() -> None:
    ap = argparse.ArgumentParser(description="용아맥 예매 오픈 텔레그램 알리미")
    ap.add_argument("--probe", nargs="?", const="", metavar="YYYYMMDD",
                    help="API/파싱 점검")
    ap.add_argument("--setup", action="store_true", help="chat_id 찾기")
    ap.add_argument("--test", action="store_true", help="텔레그램 전송 테스트")
    ap.add_argument("--once", action="store_true", help="1회 검사 후 종료")
    ap.add_argument("--reset", action="store_true", help="저장 상태 초기화")
    a = ap.parse_args()

    if a.probe is not None:
        return do_probe(a.probe or None)
    if a.setup:
        return do_setup()
    if a.test:
        print("전송 성공" if notify(f"🔔 테스트 — {datetime.now(KST):%H:%M:%S}")
              else "전송 실패")
        return
    if a.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        print("상태 초기화 완료")

    if not BOT_TOKEN or not CHAT_ID:
        print("TG_BOT_TOKEN / TG_CHAT_ID 를 먼저 설정하세요.")
        sys.exit(1)
    watch(once=a.once)


if __name__ == "__main__":
    main()
