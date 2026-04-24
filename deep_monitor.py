#!/usr/bin/env python3
"""
Xior Lutherse Burgwal deep monitor.

Reads the main content area of the popup (not just its footer buttons),
distinguishing 'No rooms available' vs 'rooms listed with book option'.
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

ROOM_TYPES = [
    {"name": "Comfy",  "price_re": r"590"},
    {"name": "Deluxe", "price_re": r"800"},
]

STATE_FILE = Path("deep_state.json")
FAILURE_FILE = Path("deep_failures.json")
MAX_CONSECUTIVE_FAILURES = 4

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Strong signals that NOTHING is available
NOT_AVAILABLE_PHRASES = [
    r"no\s+rooms?\s+available\s+at\s+the\s+moment",
    r"no\s+rooms?\s+available",
    r"get\s+notified\s+when\s+bookings?\s+open",
    r"unable\s+to\s+fill\s+in\s+your\s+search\s+query",
    r"keep\s+an\s+eye\s+on\s+this\s+website",
    r"if\s+something\s+becomes\s+available",
    r"currently\s+unavailable",
    r"fully\s+booked",
    r"volzet",
    r"sold\s+out",
]

# These words only mean "available" if they appear alongside per-room detail
# (move-in date, price, room number). A standalone "Start booking" button
# does NOT count.
STRONG_AVAILABLE_PATTERNS = [
    r"move[-\s]*in\s+date",
    r"available\s+from",
    r"available\s+starting",
    r"€\s*\d+.*per\s+month",
    r"choose\s+your\s+contract",
    r"select\s+your\s+contract",
    r"room\s+\d+",
    r"studio\s+\d+",
    r"floor\s+\d+",
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
    strategies = [
        lambda: page.get_by_text(re.compile(rf"{room_name}\s+from\s+.*{price_re}", re.I)).first,
        lambda: page.get_by_role("button", name=re.compile(rf"{room_name}", re.I)).first,
        lambda: page.get_by_text(re.compile(rf"^\s*{room_name}\s*$", re.I)).first,
        lambda: page.locator(f'text=/{price_re}/').first,
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
    log("    Next not clicked")
    return False


def capture_full_popup(page) -> str:
    """
    Grab the full popup/modal text. We take the LARGEST visible modal container,
    not the smallest, because we want header + body + footer as one blob.
    """
    candidates: list[tuple[int, str]] = []

    css_selectors = [
        '[role="dialog"]',
        '[aria-modal="true"]',
        '.modal',
        '[class*="modal"]',
        '[class*="Modal"]',
        '[class*="popup"]',
        '[class*="Popup"]',
        '[class*="dialog"]',
        '[class*="Dialog"]',
        '[class*="overlay"]',
        '[class*="Overlay"]',
    ]
    for sel in css_selectors:
        try:
            locs = page.locator(sel).all()
            for loc in locs:
                try:
                    if not loc.is_visible(timeout=300):
                        continue
                    t = loc.text_content(timeout=1500) or ""
                    t = re.sub(r"\s+", " ", t).strip()
                    if len(t) > 10:
                        candidates.append((len(t), t))
                except Exception:
                    continue
        except Exception:
            continue

    # Also try walking up from known popup anchors (header, "No rooms available", "Get notified")
    anchors = [
        r"select\s+your\s+room\s+type",
        r"no\s+rooms?\s+available",
        r"get\s+notified\s+when",
    ]
    for phrase in anchors:
        try:
            loc = page.get_by_text(re.compile(phrase, re.I)).first
            for levels in range(3, 9):
                try:
                    ancestor = loc.locator(f"xpath=ancestor::*[{levels}]")
                    t = ancestor.text_content(timeout=1000) or ""
                    t = re.sub(r"\s+", " ", t).strip()
                    if 30 < len(t) < 6000:
                        candidates.append((len(t), t))
                except Exception:
                    continue
        except Exception:
            continue

    if candidates:
        # Pick the largest candidate that's still under ~6000 chars
        # (that's the full popup, not the whole page)
        candidates = [(l, t) for (l, t) in candidates if l < 6000]
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

    # Fallback: page body (not ideal but better than empty)
    try:
        text = page.locator("body").text_content(timeout=3000) or ""
        return re.sub(r"\s+", " ", text).strip()[:6000]
    except Exception:
        return ""


def analyze(text: str) -> dict:
    low = text.lower()
    not_avail = [p for p in NOT_AVAILABLE_PHRASES if re.search(p, low)]
    strong_avail = [p for p in STRONG_AVAILABLE_PATTERNS if re.search(p, low)]
    # Need a strong availability signal AND no "not available" blocker
    likely_available = bool(strong_avail) and not not_avail
    return {
        "not_avail_hits": not_avail,
        "strong_avail_hits": strong_avail,
        "likely_available": likely_available,
    }


def run_flow(page, room: dict) -> dict:
    name = room["name"]
    log(f"\n=== Flow: {name} ===")
    result = {"room_type": name}

    try:
        log(f"  [goto] {MARKETING_URL}")
        page.goto(MARKETING_URL, wait_until="domcontentloaded", timeout=45000)
        log(f"  [goto] OK")
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
        return result

    if not wait_for_popup(page, 15000):
        result["error"] = "popup never appeared"
        return result

    page.wait_for_timeout(1500)

    if not click_room_card(page, name, room["price_re"]):
        result["error"] = f"{name} card not clickable"
        return result

    page.wait_for_timeout(2000)
    click_next(page)
    page.wait_for_timeout(5000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    captured = capture_full_popup(page)
    log(f"  [popup capture] len={len(captured)}")
    log(f"  [popup sample] {captured[:600]!r}")
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
                    f"Signals: {', '.join(info['strong_avail_hits'])}\n"
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
                    f"📝 <b>{room['name']} popup changed</b>\n"
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
        log(f"  {k}: avail={info.get('likely_available')} "
            f"not_avail_hits={info.get('not_avail_hits')} "
            f"strong_avail_hits={info.get('strong_avail_hits')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
