"""
DART 공시 -> 텔레그램 알림
관심 종목의 새 공시가 올라오면 텔레그램으로 보내줍니다.
"""

import os
import json
import html
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ============================================================
# 여기만 고치면 됩니다. 회사 이름을 넣으세요 (종목코드 아님)
# ============================================================
WATCH = [
    "삼성전자",
    "SK하이닉스",
    "에스티팜",
    "신한지주",
    "셀트리온",
    "한화오션"
    "LG에너지솔루션",
]
# ============================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_KEY = os.environ["DART_API_KEY"]

SEEN_FILE = Path("seen.json")
KST = timezone(timedelta(hours=9))


def send(text):
    """텔레그램으로 메시지 한 통 보내기."""
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
    """오늘 올라온 공시를 전부 가져옵니다."""
    today = datetime.now(KST).strftime("%Y%m%d")
    items = []
    page = 1

    while page <= 30:  # 안전장치
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
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

        if status == "013":  # 조회된 데이터 없음
            break
        if status != "000":
            print("DART 오류:", status, data.get("message"))
            break

        items.extend(data.get("list", []))
        if page >= data.get("total_page", 1):
            break
        page += 1

    return items


def main():
    first_run = not SEEN_FILE.exists()
    seen = set(json.loads(SEEN_FILE.read_text())) if not first_run else set()

    items = fetch_today()
    print(f"오늘 공시 {len(items)}건 확인")

    # 관심 종목 것만 골라내기
    hits = [
        it for it in items
        if any(name in it.get("corp_name", "") for name in WATCH)
    ]

    new = [it for it in hits if it["rcept_no"] not in seen]

    if first_run:
        # 첫 실행에 하루치가 한꺼번에 쏟아지는 걸 방지
        for it in items:
            seen.add(it["rcept_no"])
        send(
            "✅ <b>공시 알림 설치 완료</b>\n\n"
            f"감시 종목: {', '.join(WATCH)}\n"
            f"오늘 접수된 전체 공시: {len(items)}건\n"
            f"그중 감시 종목: {len(hits)}건\n\n"
            "지금부터 새 공시가 올라오면 알려드릴게요."
        )
    else:
        for it in new:
            corp = html.escape(it.get("corp_name", ""))
            title = html.escape(it.get("report_nm", ""))
            filer = html.escape(it.get("flr_nm", ""))
            link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}"
            send(f"📢 <b>{corp}</b>\n{title}\n\n제출: {filer}\n{link}")
            seen.add(it["rcept_no"])
        print(f"새 공시 {len(new)}건 발송")

    # 기록 저장 (너무 커지지 않게 최근 3000개만)
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-3000:], ensure_ascii=False))


if __name__ == "__main__":
    main()
