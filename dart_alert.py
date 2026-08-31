"""
DART 공시 -> 텔레그램 알림 (v6)
- DART 서버가 느릴 때 3번까지 재시도하고, 그래도 안 되면 조용히 건너뜁니다
"""

import os
import io
import json
import html
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_KEY = os.environ["DART_API_KEY"]

MANUAL = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

SEEN_FILE = Path("seen.json")
WATCH_FILE = Path("watchlist.txt")
KST = timezone(timedelta(hours=9))
BASE = "https://opendart.fss.or.kr/api"

DETAIL_MAP = [
    ("자기주식취득 신탁계약 해지", "tsstkAqTrctrCcDecsn"),
    ("자기주식취득 신탁계약 체결", "tsstkAqTrctrCnsDecsn"),
    ("자기주식 취득", "tsstkAqDecsn"),
    ("자기주식취득", "tsstkAqDecsn"),
    ("자기주식 처분", "tsstkDpDecsn"),
    ("자기주식처분", "tsstkDpDecsn"),
    ("유무상증자", "pifricDecsn"),
    ("유상증자", "piicDecsn"),
    ("무상증자", "fricDecsn"),
    ("전환사채", "cvbdIsDecsn"),
    ("신주인수권부사채", "bdwtIsDecsn"),
    ("교환사채", "exbdIsDecsn"),
    ("감자", "crDecsn"),
]

LABELS = {
    "nstk_ostk_cnt": "신주 보통주식수",
    "nstk_estk_cnt": "신주 기타주식수",
    "fv_ps": "액면가",
    "bfic_tisstk_ostk": "증자전 발행주식수",
    "fdpp_fclt": "시설자금",
    "fdpp_op": "운영자금",
    "fdpp_dtrp": "채무상환자금",
    "fdpp_ocsa": "타법인증권 취득자금",
    "fdpp_etc": "기타자금",
    "ic_mthn": "증자방식",
    "nstk_asstd": "신주 배정기준일",
    "nstk_dividrd": "신주 배당기산일",
    "nstk_dlprd": "신주 상장예정일",
    "aqpln_stk_ostk": "취득예정 보통주식수",
    "aqpln_stk_estk": "취득예정 기타주식수",
    "aqpln_prc_ostk": "취득예정 금액",
    "aqexpd_bgd": "취득 시작일",
    "aqexpd_edd": "취득 종료일",
    "hdexpd_bgd": "보유 시작일",
    "hdexpd_edd": "보유 종료일",
    "aq_pp": "취득 목적",
    "aq_mth": "취득 방법",
    "cs_iv_bk": "위탁 증권사",
    "bd_tm": "회차",
    "bd_knd": "사채 종류",
    "bd_fta": "사채 총액",
    "bd_intr_ex": "표면이자율",
    "bd_intr_sf": "만기이자율",
    "bd_mtd": "만기일",
    "cv_prc": "전환가액",
    "cv_rt": "전환비율",
    "cv_prd_bgd": "전환청구 시작일",
    "cv_prd_edd": "전환청구 종료일",
    "bddd": "이사회 결의일",
}

SKIP = {"rcept_no", "corp_cls", "corp_code", "corp_name", "status", "message"}

# 제목에 이 단어가 있으면 알림을 보내지 않습니다
IGNORE = [
    "특정증권등소유상황보고서",
    "대량보유상황보고서",
    "기업설명회",
    "대규모기업집단현황공시",
    "분기보고서",
    "반기보고서",
]


def send(text):
    if len(text) > 3900:
        text = text[:3900] + "\n\n(내용이 길어 잘렸습니다)"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print("텔레그램 전송 실패:", r.text)
    except Exception as e:
        print("텔레그램 접속 실패:", e)


def load_watchlist():
    exact, partial = set(), []
    if not WATCH_FILE.exists():
        print("watchlist.txt 가 없습니다")
        return exact, partial

    for line in WATCH_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.strip('",\'[] \t')
        if not line:
            continue
        if line.endswith("*"):
            partial.append(line[:-1].strip())
        else:
            exact.add(line)

    return exact, partial


def matches(corp_name, exact, partial):
    if corp_name in exact:
        return True
    return any(p and p in corp_name for p in partial)


def dart_get(path, params, tries=3):
    """DART 호출. 실패하면 잠시 쉬었다 다시 시도합니다."""
    params = dict(params, crtfc_key=DART_KEY)
    for attempt in range(tries):
        try:
            return requests.get(f"{BASE}/{path}", params=params, timeout=30)
        except Exception as e:
            print(f"  DART 접속 실패 ({attempt + 1}/{tries}):", e)
            if attempt < tries - 1:
                time.sleep(10)
    return None


def verify_watchlist(exact, partial):
    r = dart_get("corpCode.xml", {}, tries=2)
    if r is None:
        send("⚠️ 회사 명부를 받지 못해 점검을 건너뜁니다.")
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except Exception as e:
        print("회사 명부 해석 실패:", e)
        send("⚠️ 회사 명부를 읽지 못해 점검을 건너뜁니다.")
        return

    all_names = [
        (el.findtext("corp_name") or "").strip() for el in root.iter("list")
    ]
    name_set = set(all_names)

    ok, bad = [], []

    for nm in sorted(exact):
        if nm in name_set:
            ok.append(f"✔ {nm}")
        else:
            near = [n for n in all_names if nm in n][:3]
            hint = f" (혹시 {', '.join(near)}?)" if near else ""
            bad.append(f"✘ {nm} — 이 이름의 회사가 없습니다{hint}")

    for nm in partial:
        found = [n for n in all_names if nm in n]
        if not found:
            bad.append(f"✘ {nm}* — 걸리는 회사가 없습니다")
        elif len(found) > 8:
            bad.append(f"⚠ {nm}* — {len(found)}곳이 걸립니다. 너무 많습니다")
        else:
            ok.append(f"✔ {nm}* → {', '.join(found)}")

    msg = f"🔎 <b>감시 목록 점검</b>\n\n정상 {len(ok)}곳"
    if bad:
        msg += f" / 확인 필요 {len(bad)}곳\n\n" + "\n".join(bad)
    else:
        msg += "\n\n전부 정상입니다."
    if ok:
        msg += "\n\n" + "\n".join(ok)
    send(msg)


def fetch_today():
    """오늘 공시 목록. 서버에 못 붙으면 None 을 돌려줍니다."""
    today = datetime.now(KST).strftime("%Y%m%d")
    items, page = [], 1

    while page <= 30:
        r = dart_get(
            "list.json",
            {
                "bgn_de": today,
                "end_de": today,
                "page_no": page,
                "page_count": 100,
            },
        )
        if r is None:
            print("DART 서버에 연결하지 못했습니다. 이번 회차는 건너뜁니다.")
            return None

        try:
            data = r.json()
        except Exception as e:
            print("DART 응답 해석 실패:", e)
            return None

        status = data.get("status")
        if status == "013":
            break
        if status != "000":
            print("DART 목록 오류:", status, data.get("message"))
            break

        items.extend(data.get("list", []))
        if page >= data.get("total_page", 1):
            break
        page += 1

    return items


def pick_endpoint(report_nm):
    for keyword, endpoint in DETAIL_MAP:
        if keyword in report_nm:
            return endpoint
    return None


def fetch_detail(endpoint, corp_code, rcept_no):
    today = datetime.now(KST).strftime("%Y%m%d")
    r = dart_get(
        f"{endpoint}.json",
        {"corp_code": corp_code, "bgn_de": today, "end_de": today},
        tries=2,
    )
    if r is None:
        return None

    try:
        data = r.json()
    except Exception as e:
        print(f"  상세 응답 해석 실패 ({endpoint}):", e)
        return None

    if data.get("status") != "000":
        print(f"  상세 없음 ({endpoint}):", data.get("status"), data.get("message"))
        return None

    for row in data.get("list", []):
        if row.get("rcept_no") == rcept_no:
            return row
    return None


def format_detail(row):
    lines = []
    for key, value in row.items():
        if key in SKIP or value in (None, "", "-"):
            continue
        lines.append(f"· {LABELS.get(key, key)}: {html.escape(str(value))}")
        if len(lines) >= 18:
            lines.append("· ...")
            break
    return "\n".join(lines)


def main():
    exact, partial = load_watchlist()
    print(f"감시 대상: 정확히 {len(exact)}곳, 부분일치 {len(partial)}건")

    if MANUAL:
        verify_watchlist(exact, partial)

    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()

    items = fetch_today()
    if items is None:
        return

    print(f"오늘 전체 공시 {len(items)}건")

    hits = [
        it for it in items
        if matches(it.get("corp_name", ""), exact, partial)
        and not any(w in it.get("report_nm", "") for w in IGNORE)
    ]
    new = [it for it in hits if it["rcept_no"] not in seen]

    if first_run:
        for it in items:
            seen.add(it["rcept_no"])
        send(f"✅ <b>알림 시작</b>\n오늘 전체 {len(items)}건 / 감시 대상 {len(hits)}건")
        SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:], ensure_ascii=False))
        return

    for it in new:
        corp = html.escape(it.get("corp_name", ""))
        title = html.escape(it.get("report_nm", ""))
        link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"
        print(f"처리 중: {corp} - {title}")

        detail_text = ""
        endpoint = pick_endpoint(it.get("report_nm", ""))
        if endpoint:
            row = fetch_detail(endpoint, it["corp_code"], it["rcept_no"])
            if row:
                detail_text = "\n\n" + format_detail(row)

        send(f"📢 <b>{corp}</b>\n{title}{detail_text}\n\n{link}")
        seen.add(it["rcept_no"])

    print(f"새 공시 {len(new)}건 발송")
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:], ensure_ascii=False))


if __name__ == "__main__":
    main()
