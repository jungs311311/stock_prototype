"""
DART 공시 -> 텔레그램 알림 (v3)

- 감시 종목은 watchlist.txt 에서 읽습니다
- 종목코드로 정확히 매칭합니다
- 손으로 직접 실행하면(Run workflow) 목록에 오타가 없는지 점검해서 알려줍니다
"""

import os
import io
import json
import html
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_KEY = os.environ["DART_API_KEY"]

# 손으로 실행했는지, 예약 실행인지 구분
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


def send(text):
    if len(text) > 3900:
        text = text[:3900] + "\n\n(내용이 길어 잘렸습니다)"
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if r.status_code != 200:
        print("텔레그램 전송 실패:", r.text)


def load_watchlist():
    """watchlist.txt 를 읽어 종목코드 집합과 회사명 목록으로 나눕니다."""
    codes, names = {}, []
    if not WATCH_FILE.exists():
        print("watchlist.txt 가 없습니다")
        return codes, names

    for line in WATCH_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        first = parts[0]
        if first.isdigit() and len(first) == 6:
            memo = " ".join(parts[1:]) if len(parts) > 1 else first
            codes[first] = memo
        else:
            names.append(line)

    return codes, names


def verify_watchlist(codes, names):
    """DART 전체 회사 목록을 받아 오타를 점검합니다. 손으로 실행할 때만 돕니다."""
    try:
        r = requests.get(
            f"{BASE}/corpCode.xml",
            params={"crtfc_key": DART_KEY},
            timeout=120,
        )
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except Exception as e:
        print("회사 목록 내려받기 실패:", e)
        return

    listed = {}   # 종목코드 -> 회사명
    all_names = []
    for el in root.iter("list"):
        name = (el.findtext("corp_name") or "").strip()
        stock = (el.findtext("stock_code") or "").strip()
        all_names.append(name)
        if stock:
            listed[stock] = name

    ok_lines, bad_lines = [], []

    for code, memo in codes.items():
        real = listed.get(code)
        if real:
            mark = "" if (memo == code or memo == real) else f"  (적어두신 이름: {memo})"
            ok_lines.append(f"✔ {code} {real}{mark}")
        else:
            bad_lines.append(f"✘ {code} — 상장 종목코드에 없습니다")

    for nm in names:
        matched = [n for n in all_names if nm in n]
        if not matched:
            bad_lines.append(f"✘ {nm} — 이런 이름의 회사가 없습니다")
        elif len(matched) > 5:
            bad_lines.append(f"⚠ {nm} — {len(matched)}개 회사가 걸립니다. 너무 많이 옵니다")
        else:
            ok_lines.append(f"✔ {nm} → {', '.join(matched)}")

    msg = f"🔎 <b>감시 목록 점검</b>\n\n정상 {len(ok_lines)}건"
    if bad_lines:
        msg += f" / 확인 필요 {len(bad_lines)}건\n\n" + "\n".join(bad_lines)
    else:
        msg += "\n\n전부 정상입니다."
    msg += "\n\n" + "\n".join(ok_lines)
    send(msg)


def fetch_today():
    today = datetime.now(KST).strftime("%Y%m%d")
    items, page = [], 1

    while page <= 30:
        r = requests.get(
            f"{BASE}/list.json",
            params={
                "crtfc_key": DART_KEY,
                "bgn_de": today,
                "end_de": today,
                "page_no": page,
                "page_count": 100,
            },
            timeout=20,
        )
        data = r.json()
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
    try:
        r = requests.get(
            f"{BASE}/{endpoint}.json",
            params={
                "crtfc_key": DART_KEY,
                "corp_code": corp_code,
                "bgn_de": today,
                "end_de": today,
            },
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        print(f"  상세 조회 실패 ({endpoint}):", e)
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
    codes, names = load_watchlist()
    print(f"감시 종목: 코드 {len(codes)}개, 이름 {len(names)}개")

    if MANUAL:
        verify_watchlist(codes, names)

    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()

    items = fetch_today()
    print(f"오늘 전체 공시 {len(items)}건")

    hits = []
    for it in items:
        stock = (it.get("stock_code") or "").strip()
        corp = it.get("corp_name", "")
        if stock in codes or any(nm in corp for nm in names):
            hits.append(it)

    new = [it for it in hits if it["rcept_no"] not in seen]

    if first_run:
        for it in items:
            seen.add(it["rcept_no"])
        send(f"✅ <b>알림 시작</b>\n오늘 전체 {len(items)}건 / 감시 종목 {len(hits)}건")
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
