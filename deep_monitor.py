#!/usr/bin/env python3
"""
Xior Lutherse Burgwal DEEP monitor.
Uses Playwright (headless Chromium) to render the xior-booking.com
booking engine pages and detect real-time availability changes.

Runs alongside the fast hash-based monitor.py but checks the actual
booking engine rather than the marketing page.
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

# ---------- config ----------
# Note: space IDs for Lutherse Burgwal. 6089 confirmed in public search
# results; 6122 is a best-guess for the short-stay variant. If one
# returns 404 consistently, email denhaag@xior.nl asking for the direct URL.
TARGETS = [
    {
        "key": "booking_long_stay",
        "url": "https://www.xior-booking.com/space/6089/lutherse-burgwal",
        "label": "Booking engine — Lutherse Burgwal (long stay)",
    },
    {
        "key": "booking_short_stay",
        "url": "https://www.xior-booking.com/space/6122/lutherse-burgwal-short-stay",
        "label": "Booking engine — Lutherse Burgwal (short stay)",
    },
]

STATE_FILE = Path("deep_state.json")
FAILURE_FILE = Path("deep_failures.json")
MAX_CONSECUTIVE_FAILURES = 4

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Signals on a rendered booking page that indicate REAL availability
AVAILABLE_PATTERNS = [
    r"\bbook\s*now\b",
    r"\bselect\s*(?:room|unit|studio)\b",
    r"\badd\s*to\s*(?:cart|booking)\b",
    r"\breserve\s*now\b",
    r"\bavailable\s*rooms?\b",
    r"\bavailable\s*units?\b",
    r"\bavailable\s*studios?\b",
    r"\bapply\s*now\b",
]

NOT_AVAILABLE_PATTERNS = [
    r"\bfully\s*booked\b",
    r"\bno\s*(?:rooms?|units?|studios?)\s*available\b",
    r"\bsold\s*out\b",
    r"\bvolzet\b",
    r"\bnot\s*available\b",
    r"\bwaitlist\s*only\b",
]

QUEUE_PATTERNS = [
    r"\byou\s*are\s*now\s*in\s*line\b",
    r"\bin\s*de\s*wachtrij\b",
    r"\bthank\s*you\s*for\s*your\s*patience\b",
    r"\bqueue\s*position\b",
    r"\bcloudflare\b.*\bchecking\b",
]

ROOM_COUNT_RE = re.compile(
    r"(\d+)\s*(?:rooms?|units?|studios?|kamers?)\s*(?:available|beschikbaar|left)",
    re.IGNORECASE,
)
PRICE_RE = re.compile(r"€\s*([0-9][0-9.,]*)")


# ---------- telegram ----------
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


# ---------- helpers ----------
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


def analyze(text: str) -> dict:
    low = text.lower()
    in_queue = any(re.search(p, low) for p in QUEUE_PATTERNS)
    available_hits = [p for p in AVAILABLE_PATTERNS if re.search(p, low)]
    unavailable_hits = [p for p in NOT_AVAILABLE_PATTERNS if re.search(p, low)]

    room_counts = [int(m.group(1)) for m in ROOM_COUNT_RE.finditer(text)]
    prices = PRICE_RE.findall(text)
    non_zero_prices = [
        p for p in prices if re.sub(r"[.,]", "", p).lstrip("0") != ""
    ]

    likely_available = (
        not in_queue
        and bool(available_hits)
        and not unavailable_hits
    )
    return {
        "in_queue": in_queue,
        "available_hits": available_hits,
        "unavailable_hits": unavailable_hits,
        "room_counts": room_counts,
        "prices": prices[:10],
        "non_zero_prices": non_zero_prices[:10],
        "likely_available": likely_available,
    }


def playwright_fetch(url: str, page) -> tuple[str, int]:
    """Returns (rendered_visible_text, http_status). Status -1/-2 on exception."""
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = response.status if response else 0

        # Best-effort cookie banner dismissal
        for selector in [
            'button:has-text("Accept")',
            'button:has-text("Agree")',
            'button:has-text("OK")',
            'button:has-text("Alles accepteren")',
            'button:has-text("Akkoord")',
            '[aria-label*="accept" i]',
        ]:
            try:
                page.locator(selector).first.click(timeout=1500)
                break
            except Exception:
                continue

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

        body = page.text_content("body") or ""
        body = re.sub(r"\s+", " ", body).strip()
        return body, status
    except PWTimeout:
        return "", -1
    except Exception as e:
        print(f"[pw] error on {url}: {e}", file=sys.stderr)
        return "", -2


# ---------- main ----------
def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prev_state = load_json(STATE_FILE, {})
    failures = load_json(FAILURE_FILE, {"count": 0, "last_error": ""})
    new_state: dict = {}
    alerts: list[str] = []
    fetch_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
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
        page = context.new_page()

        for target in TARGETS:
            key = target["key"]
            url = target["url"]
            label = target["label"]

            text, status = playwright_fetch(url, page)
            if not text or status < 200 or status >= 400:
                fetch_errors.append(f"{key}: status={status}")
                if key in prev_state:
                    new_state[key] = prev_state[key]
                continue

            info = analyze(text)
            h = digest(text)
            new_state[key] = {
                "checked_at": now,
                "hash": h,
                "url": url,
                "label": label,
                "info": info,
                "length": len(text),
                "status": status,
            }

            prev = prev_state.get(key, {})
            prev_info = prev.get("info", {})
            was_available = bool(prev_info.get("likely_available"))
            is_available = bool(info["likely_available"])
            was_in_queue = bool(prev_info.get("in_queue"))
            is_in_queue = bool(info["in_queue"])
            prev_total = sum(prev_info.get("room_counts", []) or [])
            curr_total = sum(info["room_counts"] or [])

            # HIGH priority: flipped from not-available to available
            if is_available and not was_available and not is_in_queue:
                price_str = (
                    "€" + info["non_zero_prices"][0]
                    if info["non_zero_prices"] else "price not parsed"
                )
                alerts.append(
                    f"🚨 <b>ROOM AVAILABLE — {label}</b>\n"
                    f"Price: {price_str}\n"
                    f"Rooms visible: {curr_total if info['room_counts'] else 'count not parsed'}\n"
                    f"Link: {url}\n\n"
                    f"➡ Open NOW, book immediately.\n"
                    f"➡ €75 booking fee + 2mo deposit + 1st month within 5 days."
                )
            # MEDIUM: room count went up
            elif curr_total > prev_total and curr_total > 0:
                alerts.append(
                    f"📈 <b>Room count up — {label}</b>\n"
                    f"{prev_total} → {curr_total} rooms\n"
                    f"Link: {url}"
                )
            # INFO: first successful read after queue cleared
            elif was_in_queue and not is_in_queue:
                alerts.append(
                    f"✅ <b>Out of queue — {label}</b>\n"
                    f"Site responsive. Available: {is_available}\n"
                    f"Link: {url}"
                )

        browser.close()

    if alerts:
        header = f"🔍 <b>Xior deep monitor</b> — {now}\n\n"
        tg_send(header + "\n\n---\n\n".join(alerts))

    if fetch_errors:
        failures["count"] = failures.get("count", 0) + 1
        failures["last_error"] = "; ".join(fetch_errors)
        if failures["count"] == MAX_CONSECUTIVE_FAILURES:
            tg_send(
                f"⚠ Deep monitor: {failures['count']} consecutive failed runs.\n"
                f"Last error: {failures['last_error'][:300]}\n"
                f"Check GitHub Actions logs. Space IDs may be wrong or "
                f"Cloudflare may be blocking."
            )
    else:
        failures = {"count": 0, "last_error": ""}

    save_json(STATE_FILE, new_state)
    save_json(FAILURE_FILE, failures)

    print(f"[{now}] urls={len(TARGETS)} errors={len(fetch_errors)} alerts={len(alerts)}")
    for k, v in new_state.items():
        info = v.get("info", {})
        print(f"  {k}: avail={info.get('likely_available')} "
              f"queue={info.get('in_queue')} hash={v.get('hash')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
