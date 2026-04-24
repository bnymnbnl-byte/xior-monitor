#!/usr/bin/env python3
"""
Xior Lutherse Burgwal deep monitor — modal click-through version.

Opens the Lutherse Burgwal page, clicks 'Start your application',
selects each room type (Comfy, Deluxe), clicks Next, and captures
the resulting availability text. Alerts on any meaningful change.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

MARKETING_URL = (
    "https://www.xiorstudenthousing.eu/netherlands/the-hague/"
    "lutherse-burgwal-student-accommodation/"
)
ROOM_TYPES = ["Comfy", "Deluxe"]

STATE_FILE = Path("deep_state.json")
FAILURE_FILE = Path("deep_failures.json")
MAX_CONSECUTIVE_FAILURES = 4

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Phrases Xior uses when nothing can be booked.
NOT_AVAILABLE_PHRASES = [
    r"unable\s+to\s+fill\s+in\s+your\s+search\s+query",
    r"keep\s+an\s+eye\s+on\s+this\s+website",
    r"if\s+something\s+becomes\s+available",
    r"no\s+rooms?\s+available",
    r"currently\s+unavailable",
    r"fully\s+booked",
    r"volzet",
    r"sold\s+out",
]

# Phrases that only appear when a real room can be booked.
AVAILABLE_PHRASES = [
    r"start\s+booking",
    r"continue\s+to\s+booking",
    r"proceed\s+to\s+booking",
    r"add\s+to\s+cart",
    r"select\s+your\s+room",
]


def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] telegram creds missing", file=sys.stderr)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"[tg] {r.status_code}: {r.text[:300]}", file=sys.stderr)
    except Exception as e:
        print(f"[tg] send failed: {e}", file=sys.stderr)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def dismiss_cookies(page) -> None:
    for sel in [
        'button:has-text("Accept")',
        'button:has-text("Agree")',
        'button:has-text("Alles accepteren")',
        'button:has-text("Akkoord")',
        'button:has-text("OK")',
    ]:
        try:
            page.locator(sel).first.click(timeout=1200)
            return
        except Exception:
            continue


def try_click(page, texts, timeout=3500) -> bool:
    for t in texts:
        try:
            page.get_by_role("button", name=re.compile(t, re.IGNORECASE)).first.click(timeout=timeout)
            return True
        except Exception:
            pass
        try:
            page.get_by_text(re.compile(t, re.IGNORECASE), exact=False).first.click(timeout=timeout)
            return True
        except Exception:
            pass
        for sel in [
            f'button:has-text("{t}")',
            f'a:has-text("{t}")',
            f'[role="button"]:has-text("{t}")',
        ]:
            try:
                page.locator(sel).first.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


def capture_visible(page) -> str:
    try:
        text = page.locator("body").text_content(timeout=3000) or ""
    except Exception:
        text = ""
    return re.sub(r"\s+", " ", text).strip()


def analyze(text: str) -> dict:
    low = text.lower()
    not_avail = [p for p in NOT_AVAILABLE_PHRASES if re.search(p, low)]
    avail = [p for p in AVAILABLE_PHRASES if re.search(p, low)]
    likely_available = bool(avail) and not not_avail
    return {
        "not_avail_hits": not_avail,
        "avail_hits": avail,
        "likely_available": likely_available,
    }


def run_flow(page, room_type: str) -> dict:
    result = {"room_type": room_type}
    try:
        page.goto(MARKETING_URL, wait_until="domcontentloaded", timeout=45000)
    except PWTimeout:
        result["error"] = "nav timeout"
        return result

    dismiss_cookies(page)
    page.wait_for_timeout(2000)

    if not try_click(page, ["Start your application", "Start application"]):
        if not try_click(page, ["Check availability", "Check Availability"]):
            result["error"] = "no Start button"
            result["captured"] = capture_visible(page)[:2000]
            return result

    page.wait_for_timeout(3500)

    if not try_click(page, [room_type]):
        result["error"] = f"no {room_type} card"
        result["captured"] = capture_visible(page)[:2000]
        return result

    page.wait_for_timeout(2500)

    try_click(page, ["Next"])
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    result["captured"] = capture_visible(page)[:4000]
    return result


def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prev_state = load_json(STATE_FILE, {})
    failures = load_json(FAILURE_FILE, {"count": 0, "last_error": ""})
    new_state: dict = {}
    alerts: list[str] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-GB",
            timezone_id="Europe/Amsterdam",
        )

        for room_type in ROOM_TYPES:
            page = context.new_page()
            try:
                res = run_flow(page, room_type)
            except Exception as e:
                res = {"room_type": room_type, "error": f"{type(e).__name__}: {e}"}
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            key = f"flow_{room_type.lower()}"

            if res.get("error"):
                errors.append(f"{room_type}: {res['error']}")
                if key in prev_state:
                    new_state[key] = prev_state[key]
                continue

            text = res.get("captured", "")
            info = analyze(text)
            h = digest(text)

            new_state[key] = {
                "checked_at": now,
                "room_type": room_type,
                "hash": h,
                "info": info,
                "length": len(text),
                "sample": text[:400],
            }

            prev = prev_state.get(key, {})
            prev_info = prev.get("info", {})
            prev_hash = prev.get("hash")

            was_available = bool(prev_info.get("likely_available"))
            is_available = bool(info["likely_available"])
            prev_not_avail = set(prev_info.get("not_avail_hits", []))
            curr_not_avail = set(info["not_avail_hits"])

            if is_available and not was_available:
                alerts.append(
                    f"🚨 <b>ROOM AVAILABLE — {room_type}</b>\n"
                    f"Signals: {', '.join(info['avail_hits'])}\n"
                    f"Site: {MARKETING_URL}\n\n"
                    f"➡ Start application → pick {room_type} → Next → book now.\n"
                    f"➡ €75 booking fee + 2mo deposit + 1st month in 5 days.\n\n"
                    f"Sample: <i>{text[:300]}</i>"
                )
            elif prev and prev_not_avail and not curr_not_avail:
                alerts.append(
                    f"🚨 <b>{room_type} — 'not available' text GONE</b>\n"
                    f"Before: {', '.join(prev_not_avail)}\n"
                    f"Now: no blocker phrase. Something changed — check.\n"
                    f"{MARKETING_URL}"
                )
            elif prev_hash and prev_hash != h:
                alerts.append(
                    f"📝 <b>{room_type} flow content changed</b>\n"
                    f"Hash: {prev_hash} → {h}\n"
                    f"Sample: <i>{text[:300]}</i>\n"
                    f"{MARKETING_URL}"
                )

        browser.close()

    if alerts:
        tg_send(f"🔍 <b>Xior deep monitor</b> — {now}\n\n" + "\n\n---\n\n".join(alerts))

    if errors:
        failures["count"] = failures.get("count", 0) + 1
        failures["last_error"] = "; ".join(errors)
        if failures["count"] == MAX_CONSECUTIVE_FAILURES:
            tg_send(
                f"⚠ Deep monitor: {failures['count']} consecutive errors.\n"
                f"Last: {failures['last_error'][:300]}\n"
                f"Modal selectors may need updating."
            )
    else:
        failures = {"count": 0, "last_error": ""}

    save_json(STATE_FILE, new_state)
    save_json(FAILURE_FILE, failures)

    print(f"[{now}] flows={len(ROOM_TYPES)} errors={len(errors)} alerts={len(alerts)}")
    for k, v in new_state.items():
        info = v.get("info", {})
        print(f"  {k}: avail={info.get('likely_available')} hash={v.get('hash')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
