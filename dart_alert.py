"""
DART 공시 -> 텔레그램 알림 (상세 내용 포함)

유상증자, 자기주식 취득 같은 주요 공시는 금액과 수량까지 보여주고,
그 외 공시는 제목과 링크만 보냅니다.
"""

import os
import json
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ============================================================
# 감시할 회사 (종목코드 아니고 회사 이름)
# ============================================================
WATCH = [
    "삼성전자",
    "SK하이닉스",
    "에스티팜",
    "셀트리온",
    "한화오션",
]
# ============================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_KEY = os.environ["DART_API_KEY"]

SEEN_FILE = Path("seen.json")
KST = timezone(timedelta(hours=9))
BASE = "https://opendart.fss.or.kr/api"

# 공시 제목에 이 단어가 있으면 -> 이 상세 API를 호출
# 위에서부터 순서대로 검사하므로 순서가 중요합니다
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

# 영어 항목명을 한글로 바꿔주는 사전
# 여기 없는 항목은 영어 그대로 표시됩니다 (알려주시면 채워넣습니다)
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
    "ssl_at": "공매도 해당여부",
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
    "od_a_at_t": "사외이사 참석",
    "od_a_at_b": "사외이사 불참",
    "adt_a_atn": "감사 참석여부",
}

# 알림에 굳이 안 보여줘도 되는 항목
SKIP = {"rcept_no", "corp_cls", "corp_code", "corp_name", "status", "message"}


def send(text):
    """텔레그램으로 메시지 보내기."""
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


def fetch_today():
    """오늘 접수된 공시 목록 전체."""
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
    """공시 제목을 보고 어떤 상세 API를 쓸지 고릅니다."""
    for keyword, endpoint in DETAIL_MAP:
        if keyword in report_nm:
            return endpoint
    return None


def fetch_detail(endpoint, corp_code, rcept_no):
    """상세 내용을 가져옵니다. 실패하면 None을 돌려줍니다."""
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

    # 같은 날 여러 건이 올 수 있으니 접수번호가 일치하는 것만
    for row in data.get("list", []):
        if row.get("rcept_no") == rcept_no:
            return row
    return None


def format_detail(row):
    """상세 항목을 사람이 읽을 수 있는 형태로."""
    lines = []
    for key, value in row.items():
        if key in SKIP:
            continue
        if value in (None, "", "-"):
            continue
        label = LABELS.get(key, key)
        lines.append(f"· {label}: {html.escape(str(value))}")
        if len(lines) >= 18:
            lines.append("· ...")
            break
    return "\n".join(lines)


def main():
    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()

    items = fetch_today()
    print(f"오늘 전체 공시 {len(items)}건")

    hits = [
        it for it in items
        if any(name in it.get("corp_name", "") for name in WATCH)
    ]
    new = [it for it in hits if it["rcept_no"] not in seen]

    if first_run:
        for it in items:
            seen.add(it["rcept_no"])
        send(
            "✅ <b>공시 알림 설치 완료 (상세 버전)</b>\n\n"
            f"감시 종목: {', '.join(WATCH)}\n"
            f"오늘 전체 공시: {len(items)}건 / 감시 종목: {len(hits)}건"
        )
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
