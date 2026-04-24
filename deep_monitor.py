#!/usr/bin/env python3
"""
Xior Lutherse Burgwal deep monitor.

Flow:
  1. Load property page.
  2. Click 'Start your application' → popup opens with 'Select your room type'.
  3. Click the Comfy card (€590) or Deluxe card (€800).
  4. Click Next.
  5. Capture final screen text; alert if it flips from 'nothing available' to
     something that looks bookable.
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

# Card matching by price, since the text is "Comfy From €590,00 pm" etc.
ROOM_TYPES = [
    {"name": "Comfy",  "price_re": r"590"},
    {"name": "Deluxe", "price_re": r"800"},
]

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
    r"sorry\s+about\s+that",
]

AVAILABLE_PHRASES = [
    r"start\s+booking",
    r"continue\s+to\s+booking",
    r"proceed\s+to\s+booking",
    r"add\s+to\s+cart",
    r"reserve\s+now",
    r"book\s+this\s+room",
    r"complete\s+booking",
    r"select\s+contract",
]

POPUP_HEADER_RE = re.compile(r"select\s+your\s+room\s+type", re.IGNORECASE)


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
        'button:has-text("Accept all")',
    ]:
        try:
            page.locator(sel).first.click(timeout=1200)
            log(f"  [cookie] dismissed via {sel}")
            return
        except Exception:
            continue


def click_start_application(page) -> bool:
    log("  [click] Start your application")
    for attempt in (1, 2):
        for method in ["role_button", "role_link", "text", "css"]:
            try:
                if method == "role_button":
                    loc = page.get_by_role("button", name=re.compile(r"start\s+your\s+application", re.I)).first
                elif method == "role_link":
                    loc = page.get_by_role("link", name=re.compile(r"start\s+your\s+application", re.I)).first
                elif method == "text":
                    loc = page.get_by_text(re.compile(r"start\s+your\s+application", re.I), exact=False).first
                else:
                    loc = page.locator(
                        'button:has-text("Start your application"), '
                        'a:has-text("Start your application"), '
                        '[role="button"]:has-text("Start your application")'
                    ).first
                loc.scroll_into_view_if_needed(timeout=2500)
                loc.click(timeout=4000)
                log(f"    OK via {method} (attempt {attempt})")
                return True
            except Exception:
                continue
        page.wait_for_timeout(1500)
    log("    FAILED to click Start your application")
    return False


def wait_for_popup(page, timeout_ms: int = 15000) -> bool:
    """Wait for the 'Select your room type' heading to appear."""
    log("  [wait] popup 'Select your room type'")
    try:
        page.get_by_text(POPUP_HEADER_RE).first.wait_for(state="visible", timeout=timeout_ms)
        log("    popup visible")
        return True
    except Exception as e:
        log(f"    popup NOT visible: {type(e).__name__}")
        return False


def click_room_card(page, room_name: str, price_re: str) -> bool:
    log(f"  [click] room card {room_name} (price~{price_re})")
    # Try multiple strategies to locate the card
    strategies = [
        # 1) Full label as shown in the popup
        lambda: page.get_by_text(re.compile(rf"{room_name}\s+from\s+.*{price_re}", re.I)).first,
        # 2) Just the room name inside the popup dialog region
        lambda: page.get_by_role("button", name=re.compile(rf"{room_name}", re.I)).first,
        lambda: page.get_by_text(re.compile(rf"^\s*{room_name}\s*$", re.I)).first,
        # 3) Any element whose text contains the price — usually the card
        lambda: page.locator(f'text=/{price_re}/').first,
        # 4) CSS fallbacks
        lambda: page.locator(f'*:has-text("{room_name}"):has-text("{price_re}")').first,
    ]
    for i, strat in enumerate(strategies, 1):
        try:
            loc = strat()
            loc.scroll_into_view_if_needed(timeout=2500)
            loc.click(timeout=4000)
            log(f"    OK via strategy {i}")
            return True
        except Exception:
            continue
    log(f"    FAILED to click {room_name} card")
    return False


def click_next(page) -> bool:
    log("  [click] Next")
    for strat in [
        lambda: page.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I)).first,
        lambda: page.get_by_text(re.compile(r"^\s*next\s*$", re.I)).first,
        lambda: page.locator('button:has-text("Next")').first,
    ]:
        try:
            loc = strat()
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.click(timeout=4000)
            log("    OK")
            return True
        except Exception:
            continue
    log("    Next not clicked (may be auto-advance)")
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


def run_flow(page, room: dict) -> dict:
    name = room["name"]
    log(f"\n=== Flow: {name} ===")
    result = {"room_type": name}

    try:
        log(f"  [goto] {MARKETING_URL}")
        page.goto(MARKETING_URL, wait_until="domcontentloaded", timeout=45000)
        log(f"  [goto] OK, title={page.title()[:80]!r}")
    except PWTimeout:
        result["error"] = "nav timeout"
        return result

    dismiss_cookies(page)
    page.wait_for_timeout(1500)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    if not click_start_application(page):
        result["error"] = "Start button not clickable"
        result["captured"] = capture_visible(page)[:2000]
        return result

    if not wait_for_popup(page, 15000):
        result["error"] = "popup never appeared"
        result["captured"] = capture_visible(page)[:2000]
        return result

    # Extra settle
    page.wait_for_timeout(1500)

    if not click_room_card(page, name, room["price_re"]):
        result["error"] = f"{name} card not clickable"
        result["captured"] = capture_visible(page)[:2000]
        return result

    page.wait_for_timeout(2000)

    click_next(page)
    page.wait_for_timeout(4000)
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

        for room in ROOM_TYPES:
            page = context.new_page()
            try:
                res = run_flow(page, room)
            except Exception as e:
                res = {"room_type": room["name"], "error": f"{type(e).__name__}: {e}"}
                log(f"  [EXC] {res['error']}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            key = f"flow_{room['name'].lower()}"

            if res.get("error"):
                errors.append(f"{room['name']}: {res['error']}")
                if key in prev_state:
                    new_state[key] = prev_state[key]
                continue

            text = res.get("captured", "")
            info = analyze(text)
            h = digest(text)

            new_state[key] = {
                "checked_at": now,
                "room_type": room["name"],
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
                    f"🚨 <b>ROOM AVAILABLE — {room['name']}</b>\n"
                    f"Signals: {', '.join(info['avail_hits'])}\n"
                    f"Site: {MARKETING_URL}\n\n"
                    f"➡ Start your application → pick {room['name']} → Next → book NOW.\n"
                    f"➡ €75 booking fee + 2mo deposit + 1st month within 5 days.\n\n"
                    f"Sample: <i>{text[:300]}</i>"
                )
            elif prev and prev_not_avail and not curr_not_avail:
                alerts.append(
                    f"🚨 <b>{room['name']} — 'not available' text GONE</b>\n"
                    f"Before: {', '.join(prev_not_avail)}\n"
                    f"Now: no blocker phrase. Check the site.\n"
                    f"{MARKETING_URL}"
                )
            elif prev_hash and prev_hash != h:
                alerts.append(
                    f"📝 <b>{room['name']} flow content changed</b>\n"
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
