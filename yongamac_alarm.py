#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yongamac_alarm.py
CGV 용산아이파크몰 IMAX(용아맥) 예매 오픈 감지 -> 텔레그램 개인 알림

개인용 모니터링 도구입니다. 공개 페이지만 조회하며, 자동 예매(매크로) 기능은
의도적으로 넣지 않았습니다. 서버에 부담이 가지 않도록 폴링 간격을 지켜주세요.

사용:
    export TG_BOT_TOKEN="123456:ABC..."
    export TG_CHAT_ID="12345678"

    python3 yongamac_alarm.py --setup          # chat_id 찾기 도우미
    python3 yongamac_alarm.py --probe          # 주소/파싱 살아있는지 점검
    python3 yongamac_alarm.py --test           # 텔레그램 전송 테스트
    python3 yongamac_alarm.py                  # 감시 시작 (상시 루프)
    python3 yongamac_alarm.py --once           # 1회만 검사 (cron/Actions용)
"""

from __future__ import annotations

import argparse
import html as htmlmod
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────

KST = timezone(timedelta(hours=9))

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

AREA_CODE = os.environ.get("CGV_AREA_CODE", "01")
THEATER_CODE = os.environ.get("CGV_THEATER_CODE", "0013")   # 0013 = 용산아이파크몰
THEATER_NAME = os.environ.get("CGV_THEATER_NAME", "용아맥")

# 감시할 포맷. "IMAX" 대신 "4DX", "SCREENX" 등으로 바꾸면 그대로 재사용 가능.
FORMAT_KEYWORDS = [k.strip() for k in
                   os.environ.get("CGV_FORMATS", "IMAX,아이맥스").split(",") if k.strip()]

# 특정 영화만 보고 싶으면 지정 (비우면 전체)
TITLE_FILTER = [k.strip() for k in
                os.environ.get("CGV_TITLES", "").split(",") if k.strip()]

DAYS_AHEAD = int(os.environ.get("CGV_DAYS_AHEAD", "35"))     # 오늘부터 며칠 뒤까지 감시
POLL_SECONDS = int(os.environ.get("CGV_POLL_SECONDS", "25")) # 사이클 간격(초). 20 미만 비권장
HOT_PER_CYCLE = int(os.environ.get("CGV_HOT_PER_CYCLE", "6"))# 사이클당 '미오픈' 날짜 조회 수
COLD_EVERY = int(os.environ.get("CGV_COLD_EVERY", "20"))     # 이미 열린 날짜 재확인 주기(사이클)

TIMEOUT = 12
STATE_PATH = os.environ.get("CGV_STATE_PATH", "yongamac_state.json")

SHOWTIME_URL = (
    "http://www.cgv.co.kr/common/showtimes/iframeTheater.aspx"
    "?areacode={area}&theatercode={theater}&date={ymd}"
)
# 알림에 같이 보낼 바로가기. 실제로 본인이 쓰는 주소로 바꿔두면 제일 빠릅니다.
BOOKING_URL = "http://www.cgv.co.kr/theaters/?theaterCode={theater}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
READY_WORDS = ("예매준비중", "준비중", "오픈예정")

# 블록 경계 판정용 — "2관", "IMAX관 10층", "4DX" 같은 상영관/포맷 표기
HALL_RE = re.compile(r"^[0-9A-Za-z가-힣]{1,14}관(\s|$)")
OTHER_FMT_RE = re.compile(
    r"^(2D|3D|4DX|SCREENX|SCREEN ?X|STARIUM|SPHEREX|GOLD ?CLASS|TEMPUR|CINE ?de ?CHEF|"
    r"돌비|DOLBY|일반관|컴포트)", re.I)
SEATS_RE = re.compile(r"^\d+\s*석$")
NOISE_RE = re.compile(r"(잔여|총좌석|상영시간|©|무대인사|자막|더빙)")


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%m-%d %H:%M:%S}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────

def http_get(url: str, timeout: int = TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "http://www.cgv.co.kr/",
        "Connection": "close",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset()
    for enc in filter(None, [charset, "utf-8", "euc-kr", "cp949"]):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def tg_api(method: str, params: dict) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("TG_BOT_TOKEN 환경변수가 비어 있습니다.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def notify(text: str, silent: bool = False) -> bool:
    """텔레그램 전송. 실패해도 감시 루프는 죽이지 않는다."""
    if not CHAT_ID:
        log("!! TG_CHAT_ID 없음 — 콘솔에만 출력합니다.")
        log(text)
        return False
    for attempt in range(3):
        try:
            r = tg_api("sendMessage", {
                "chat_id": CHAT_ID,
                "text": text,
                "disable_web_page_preview": "true",
                "disable_notification": "true" if silent else "false",
            })
            if r.get("ok"):
                return True
            log(f"!! 텔레그램 응답 실패: {r}")
        except Exception as e:
            log(f"!! 텔레그램 전송 오류({attempt + 1}/3): {e}")
        time.sleep(1.5 * (attempt + 1))
    return False


# ─────────────────────────────────────────────────────────────
# 파싱
# ─────────────────────────────────────────────────────────────

def html_to_lines(raw: str) -> list[str]:
    """태그를 걷어내되 img의 alt/title은 텍스트로 살려둔다.
    (CGV는 IMAX 표시가 이미지인 경우가 있어서 alt 보존이 중요)"""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?is)<img[^>]*?(?:alt|title)\s*=\s*[\"']([^\"']*)[\"'][^>]*>", r" \1 ", s)
    s = re.sub(r"(?is)<[^>]+>", "\n", s)
    s = htmlmod.unescape(s)
    out = []
    for line in s.split("\n"):
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if line:
            out.append(line)
    return out


def analyze(raw: str) -> dict:
    """페이지에서 대상 포맷의 상태와 회차 시각을 뽑아낸다.

    마크업 클래스명에 의존하지 않고 '텍스트 근접성'으로 판단하기 때문에
    CGV가 디자인을 갈아엎어도 어지간하면 계속 동작한다.
    """
    lines = html_to_lines(raw)
    joined_lower = "\n".join(lines).lower()

    def is_fmt(ln: str) -> bool:
        return any(k.lower() in ln.lower() for k in FORMAT_KEYWORDS)

    hits = [i for i, ln in enumerate(lines) if is_fmt(ln)]
    if not hits:
        return {"state": "none", "times": [], "titles": [], "lines": len(lines)}

    # 붙어 있는 표기("IMAX" + "IMAX관 10층")는 한 덩어리로 묶는다
    groups: list[list[int]] = []
    for i in hits:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(i)
        else:
            groups.append([i])

    times: set[str] = set()
    titles: set[str] = set()
    ready = False

    for g in groups:
        # 이 포맷 블록의 회차만 읽는다. 다른 상영관 표기가 나오면 즉시 중단.
        for k in range(g[-1] + 1, min(len(lines), g[-1] + 80)):
            ln = lines[k]
            if is_fmt(ln):
                break
            if HALL_RE.match(ln) or OTHER_FMT_RE.match(ln):
                break
            for m in TIME_RE.finditer(ln):
                times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
            if any(w in ln for w in READY_WORDS):
                ready = True
        # 영화 제목은 보통 포맷 표기 바로 앞쪽에 나온다
        for ln in reversed(lines[max(0, g[0] - 40):g[0]]):
            if not (2 <= len(ln) <= 60) or ln.isdigit():
                continue
            if is_fmt(ln) or HALL_RE.match(ln) or OTHER_FMT_RE.match(ln):
                continue
            if TIME_RE.search(ln) or SEATS_RE.match(ln) or NOISE_RE.search(ln):
                continue
            titles.add(ln)
            break

    if TITLE_FILTER:
        low = [t.lower() for t in titles]
        if not any(any(f.lower() in t for t in low) for f in TITLE_FILTER):
            return {"state": "none", "times": [], "titles": [], "lines": len(lines)}

    if times:
        state = "open"
    elif ready or any(w in joined_lower for w in READY_WORDS):
        state = "ready"
    else:
        state = "listed"   # 포맷 표기는 있는데 회차가 아직 없음

    return {
        "state": state,
        "times": sorted(times),
        "titles": sorted(titles)[:4],
        "lines": len(lines),
    }


RANK = {"none": 0, "listed": 1, "ready": 2, "open": 3}


# ─────────────────────────────────────────────────────────────
# 상태 저장
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def target_dates() -> list[str]:
    today = datetime.now(KST).date()
    return [(today + timedelta(days=d)).strftime("%Y%m%d") for d in range(DAYS_AHEAD + 1)]


def pretty(ymd: str) -> str:
    d = datetime.strptime(ymd, "%Y%m%d").date()
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"


# ─────────────────────────────────────────────────────────────
# 핵심 검사
# ─────────────────────────────────────────────────────────────

def check_date(ymd: str, state: dict) -> tuple[bool, str]:
    """반환: (알림 필요 여부, 메시지)"""
    url = SHOWTIME_URL.format(area=AREA_CODE, theater=THEATER_CODE, ymd=ymd)
    info = analyze(http_get(url))

    prev = state.get(ymd, {"state": "none", "times": []})
    prev_state, prev_times = prev.get("state", "none"), set(prev.get("times", []))
    new_times = sorted(set(info["times"]) - prev_times)
    upgraded = RANK[info["state"]] > RANK[prev_state]

    state[ymd] = {"state": info["state"], "times": info["times"],
                  "seen": datetime.now(KST).isoformat(timespec="seconds")}

    # 회차가 사라지는 건(매진/시간경과) 알리지 않는다
    if not new_times and not upgraded:
        return False, ""

    # 기준선을 잡기 전이면 조용히 넘어간다
    if not state.get("_initialized"):
        return False, ""

    title = " / ".join(info["titles"]) if info["titles"] else THEATER_NAME
    if info["state"] == "open":
        head = f"🚨 예매 오픈! {pretty(ymd)}"
        body = f"{title}\n회차: {', '.join(new_times or info['times'])}"
    elif info["state"] == "ready":
        head = f"⏳ 예매 준비중 {pretty(ymd)} — 지금 앱 켜세요"
        body = title
    else:
        head = f"👀 라인업 등록 {pretty(ymd)}"
        body = title

    link = BOOKING_URL.format(theater=THEATER_CODE)
    return True, f"{head}\n{body}\n{link}"


def run_cycle(state: dict, cycle: int) -> None:
    dates = target_dates()
    hot = [d for d in dates if state.get(d, {}).get("state", "none") in ("none", "listed")]
    cold = [d for d in dates if d not in hot]

    # 미오픈 날짜(=프론티어)를 우선 조회. 새 예매는 거의 항상 여기서 열린다.
    start = (cycle * HOT_PER_CYCLE) % max(len(hot), 1)
    picks = hot[start:start + HOT_PER_CYCLE] or hot[:HOT_PER_CYCLE]
    if cold and cycle % COLD_EVERY == 0:
        picks = picks + [cold[(cycle // COLD_EVERY) % len(cold)]]

    for ymd in picks:
        try:
            hit, msg = check_date(ymd, state)
            if hit:
                log(f"** ALERT {ymd}\n{msg}")
                notify(msg)
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} @ {ymd}")
            raise
        except Exception as e:
            log(f"오류 @ {ymd}: {type(e).__name__}: {e}")
            raise
        time.sleep(random.uniform(0.6, 1.4))   # 요청 간 최소 간격


def watch(once: bool = False) -> None:
    state = load_state()
    fresh = not state.get("_initialized")
    if fresh:
        log("최초 실행 — 현재 상태를 기준선으로 잡습니다 (알림 없음)")
        for ymd in target_dates():
            try:
                info = analyze(http_get(SHOWTIME_URL.format(
                    area=AREA_CODE, theater=THEATER_CODE, ymd=ymd)))
                state[ymd] = {"state": info["state"], "times": info["times"]}
                log(f"  {pretty(ymd)}  {info['state']:6s}  {len(info['times'])}회차")
            except Exception as e:
                log(f"  {ymd} 실패: {e}")
            time.sleep(0.8)
        state["_initialized"] = True
        save_state(state)
        notify(f"✅ {THEATER_NAME} 알리미 가동 시작\n감시 포맷: {', '.join(FORMAT_KEYWORDS)}\n"
               f"감시 범위: 오늘~{DAYS_AHEAD}일 후", silent=True)

    # --once 는 매번 새 프로세스라 cycle 이 0으로 고정된다.
    # 시계로 시드를 줘서 이미 열린 날짜도 돌아가며 재확인되게 한다.
    cycle = int(time.time() // 300) if once else 0
    fails = 0
    while True:
        try:
            run_cycle(state, cycle)
            if fails >= 3:
                notify("🟢 알리미 복구됨", silent=True)
            fails = 0
            save_state(state)
        except KeyboardInterrupt:
            log("종료")
            save_state(state)
            return
        except Exception:
            fails += 1
            if fails == 3:
                notify("⚠️ 알리미가 CGV 페이지를 계속 못 읽고 있습니다. "
                       "주소가 바뀌었을 수 있어요 (--probe 확인 필요)")
            backoff = min(300, 20 * (2 ** min(fails, 4)))
            log(f"연속 실패 {fails}회 — {backoff}s 대기")
            time.sleep(backoff)
            continue

        cycle += 1
        if once:
            save_state(state)
            return
        time.sleep(POLL_SECONDS + random.uniform(-3, 3))


# ─────────────────────────────────────────────────────────────
# 보조 모드
# ─────────────────────────────────────────────────────────────

def do_setup() -> None:
    print("1) 텔레그램에서 @BotFather 검색 -> /newbot -> 토큰 복사")
    print("2) 만든 봇과 대화방을 열고 아무 메시지나 하나 보내기 ('hi')")
    print("3) TG_BOT_TOKEN 지정 후 이 명령 재실행\n")
    if not BOT_TOKEN:
        print("TG_BOT_TOKEN 이 비어 있습니다.")
        return
    r = tg_api("getUpdates", {})
    ids = []
    for u in r.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if chat.get("id"):
            ids.append((chat["id"], chat.get("title") or chat.get("first_name", "")))
    if not ids:
        print("아직 받은 메시지가 없습니다. 봇에게 먼저 말을 걸어주세요.")
        return
    for cid, name in dict.fromkeys(ids):
        print(f"  TG_CHAT_ID={cid}   ({name})")


def do_probe(ymd: str | None) -> None:
    ymd = ymd or datetime.now(KST).strftime("%Y%m%d")
    url = SHOWTIME_URL.format(area=AREA_CODE, theater=THEATER_CODE, ymd=ymd)
    print(f"GET {url}\n")
    try:
        raw = http_get(url)
    except Exception as e:
        print(f"실패: {type(e).__name__}: {e}")
        print("→ 주소가 막혔거나 바뀐 것입니다. 브라우저 개발자도구 Network 탭에서")
        print("  실제 상영시간표 요청 URL을 찾아 SHOWTIME_URL 을 교체하세요.")
        return
    lines = html_to_lines(raw)
    info = analyze(raw)
    print(f"응답 {len(raw):,}자 / 텍스트 {len(lines)}줄")
    print(f"포맷 키워드 발견: {any(any(k.lower() in l.lower() for k in FORMAT_KEYWORDS) for l in lines)}")
    print(f"분석 결과: {json.dumps(info, ensure_ascii=False)}\n")
    print("--- 텍스트 미리보기 (앞 60줄) ---")
    for l in lines[:60]:
        print("  " + l[:100])


def main() -> None:
    ap = argparse.ArgumentParser(description="용아맥 예매 오픈 텔레그램 알리미")
    ap.add_argument("--setup", action="store_true", help="chat_id 찾기")
    ap.add_argument("--probe", nargs="?", const="", metavar="YYYYMMDD",
                    help="페이지/파싱 점검")
    ap.add_argument("--test", action="store_true", help="텔레그램 전송 테스트")
    ap.add_argument("--once", action="store_true", help="1회 검사 후 종료 (cron용)")
    ap.add_argument("--reset", action="store_true", help="저장된 상태 초기화")
    a = ap.parse_args()

    if a.setup:
        return do_setup()
    if a.probe is not None:
        return do_probe(a.probe or None)
    if a.test:
        ok = notify(f"🔔 테스트 메시지 — {datetime.now(KST):%H:%M:%S}")
        print("전송 성공" if ok else "전송 실패")
        return
    if a.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        print("상태 초기화 완료")

    if not BOT_TOKEN or not CHAT_ID:
        print("TG_BOT_TOKEN / TG_CHAT_ID 를 먼저 설정하세요. (--setup 참고)")
        sys.exit(1)
    watch(once=a.once)


if __name__ == "__main__":
    main()
