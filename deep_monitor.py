#!/usr/bin/env python3
"""
Xior Lutherse Burgwal deep monitor — modal click-through version (verbose).
Logs each step so we can see where it breaks.
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

AVAILABLE_PHRASES = [
    r"start\s+booking",
    r"continue\s+to\s+booking",
    r"proceed\s+to\s+booking",
    r"add\s+to\s+cart",
    r"select\s+your\s+room",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[warn] telegram creds missing")
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
            log(f"[tg] {r.status_code}: {r.text[:300]}")
    except Exception as e:
        log(f"[tg] send failed: {e}")


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
            log(f"  [cookie] dismissed via {sel}")
            return
        except Exception:
            continue


def try_click(page, label: str, texts, timeout=4000) -> bool:
    log(f"  [click] trying {label}: {texts}")
    for t in texts:
        try:
            loc = page.get_by_role("button", name=re.compile(t, re.IGNORECASE)).first
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=timeout)
            log(f"    OK via role=button name~{t}")
            return True
        except Exception:
            pass
        try:
            loc = page.get_by_role("link", name=re.compile(t, re.IGNORECASE)).first
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=timeout)
            log(f"    OK via role=link name~{t}")
            return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(re.compile(t, re.IGNORECASE), exact=False).first
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=timeout)
            log(f"    OK via text~{t}")
            return True
        except Exception:
            pass
        for sel in [
            f'button:has-text("{t}")',
            f'a:has-text("{t}")',
            f'[role="button"]:has-text("{t}")',
            f'*[class*="button"]:has-text("{t}")',
        ]:
            try:
                loc = page.locator(sel).first
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=timeout)
                log(f"    OK via {sel}")
                return True
            except Exception:
                continue
    log(f"    FAILED to click {label}")
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
    log(f"\n=== Flow: {room_type} ===")
    result = {"room_type": room_type}

    try:
        log(f"  [goto] {MARKETING_URL}")
        page.goto(MARKETING_URL, wait_until="domcontentloaded", timeout=45000)
        log(f"  [goto] OK, title={page.title()[:80]!r}")
    except PWTimeout:
        result["error"] = "nav timeout"
        log("  [ERR] nav timeout")
        return result

    dismiss_cookies(page)
    page.wait_for_timeout(2000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    # Log a sample of the page to see what's there
    sample = capture_visible(page)[:400]
    log(f"  [page sample] {sample}")

    if not try_click(page, "Start application",
                     ["Start your application", "Start application"]):
        if not try_click(page, "Check availability",
                         ["Check availability", "Check Availability"]):
            result["error"] = "no Start/Check button found"
            result["captured"] = capture_visible(page)[:2000]
            return result

    page.wait_for_timeout(4000)
    modal_sample = capture_visible(page)[:500]
    log(f"  [after start click] {modal_sample}")

    if not try_click(page, f"{room_type} card", [room_type]):
        result["error"] = f"no {room_type} card clickable"
        result["captured"] = capture_visible(page)[:2000]
        return result

    page.wait_for_timeout(2500)

    try_click(page, "Next button", ["Next"])
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    captured = capture_visible(page)[:4000]
    log(f"  [final capture] {captured[:400]}")
    result["captured"] = captured
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
                log(f"  [ERR] exception: {res['error']}")
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
                f"Last: {failures['last_error'][:300]}"
            )
    else:
        failures = {"count": 0, "last_error": ""}

    save_json(STATE_FILE, new_state)
    save_json(FAILURE_FILE, failures)

    log(f"\n[{now}] flows={len(ROOM_TYPES)} errors={len(errors)} alerts={len(alerts)}")
    if errors:
        log("[errors]")
        for e in errors:
            log(f"  - {e}")
    for k, v in new_state.items():
        info = v.get("info", {})
        log(f"  {k}: avail={info.get('likely_available')} hash={v.get('hash')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
