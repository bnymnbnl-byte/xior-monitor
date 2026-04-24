#!/usr/bin/env python3
"""
Xior Lutherse Burgwal change-canary.

Hashes two public xiorstudenthousing.eu pages, alerts on any meaningful
content change, and flags high-priority phrases that suggest bookings
have opened for the 2026-27 academic year.

Designed to run on GitHub Actions every 15 minutes.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- config ----------
TARGETS = [
    {
        "key": "lutherse_burgwal",
        "url": "https://www.xiorstudenthousing.eu/netherlands/the-hague/lutherse-burgwal-student-accommodation/",
        "label": "Lutherse Burgwal — property page",
    },
    {
        "key": "the_hague_city",
        "url": "https://www.xiorstudenthousing.eu/netherlands/the-hague/",
        "label": "Xior The Hague — city overview",
    },
]

STATE_FILE = Path("state.json")
FAILURE_FILE = Path("failures.json")
MAX_CONSECUTIVE_FAILURES = 4

UA = (
    "XiorLutherseBurgwalMonitor/2.0 "
    "(personal student-housing change-canary; polls 2 URLs every 15min)"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# High-priority phrases — if a NEW one appears, bookings likely just opened.
BOOKING_OPEN_PATTERNS = [
    r"\bbookings?\s+(?:are\s+)?(?:now\s+)?open\b",
    r"\bbook\s+now\s+for\s+(?:2026|2027)\b",
    r"\bacademic\s+year\s+2026[-\u2011\u2013]?2027\b",
    r"\bintake\s+2026\b",
    r"\bapply\s+now\s+for\s+2026\b",
    r"\bseptember\s+2026\b",
    r"\b2026[-\u2011\u2013]2027\s+bookings?\b",
    r"\bopen\s+for\s+bookings?\b",
]
# Presence suppresses the high-priority flag.
DEFINITELY_UNAVAILABLE = [
    r"\bfully\s+booked\b",
    r"\bvolzet\b",
    r"\bno\s+rooms?\s+available\b",
    r"\bwaiting\s+list\s+only\b",
    r"\bcurrently\s+unavailable\b",
]

LB_LINE_RE = re.compile(r"lutherse\s*burgwal", re.IGNORECASE)


# ---------- telegram ----------
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[warn] Telegram credentials missing; skipping send.", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code != 200:
            print(f"[tg] {r.status_code}: {r.text[:300]}", file=sys.stderr)
    except Exception as e:
        print(f"[tg] send failed: {e}", file=sys.stderr)


# ---------- fetch ----------
def fetch(url: str) -> tuple[int, str]:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    return r.status_code, r.text


# ---------- parse ----------
def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def lb_lines(text: str) -> str:
    """Filter city-overview text down to sentences that mention Lutherse Burgwal."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return " | ".join(s for s in sentences if LB_LINE_RE.search(s))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def scan_signals(text: str) -> dict:
    low = text.lower()
    booking_open = [p for p in BOOKING_OPEN_PATTERNS if re.search(p, low)]
    unavailable = [p for p in DEFINITELY_UNAVAILABLE if re.search(p, low)]
    return {
        "booking_open_hits": booking_open,
        "unavailable_hits": unavailable,
        "high_priority": bool(booking_open) and not unavailable,
    }


# ---------- state ----------
def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------- main ----------
def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prev_state = load_json(STATE_FILE, {})
    failures = load_json(FAILURE_FILE, {"count": 0, "last_error": ""})
    new_state: dict = {}
    alerts: list[str] = []
    fetch_errors: list[str] = []

    for target in TARGETS:
        key = target["key"]
        url = target["url"]
        label = target["label"]
        try:
            status, html = fetch(url)
            if status != 200:
                fetch_errors.append(f"{key}: HTTP {status}")
                if key in prev_state:
                    new_state[key] = prev_state[key]
                continue

            full_text = clean_text(html)
            focus_text = lb_lines(full_text) if key == "the_hague_city" else full_text
            h = digest(focus_text)
            signals = scan_signals(focus_text)

            new_state[key] = {
                "checked_at": now,
                "hash": h,
                "url": url,
                "label": label,
                "signals": signals,
                "length": len(focus_text),
            }

            prev = prev_state.get(key, {})
            prev_hash = prev.get("hash")
            prev_signals = prev.get("signals", {})
            prev_booking_hits = set(prev_signals.get("booking_open_hits", []))
            now_booking_hits = set(signals["booking_open_hits"])
            new_booking_hits = now_booking_hits - prev_booking_hits

            if new_booking_hits and signals["high_priority"]:
                alerts.append(
                    f"🚨 <b>HIGH PRIORITY — booking window may be open</b>\n"
                    f"Page: {label}\n"
                    f"New phrases: {', '.join(new_booking_hits)}\n"
                    f"Link: {url}\n\n"
                    f"➡ Open the page NOW and try to book.\n"
                    f"➡ Also refresh the official 'Get notified' form on the page."
                )
            elif prev_hash and prev_hash != h:
                delta = abs(len(focus_text) - prev.get("length", 0))
                alerts.append(
                    f"📝 <b>Page content changed</b>\n"
                    f"Page: {label}\n"
                    f"Hash: {prev_hash} → {h}\n"
                    f"Length delta: {delta} chars\n"
                    f"Link: {url}\n\n"
                    f"Could be minor; could be a booking-window signal. Check."
                )

        except Exception as e:
            fetch_errors.append(f"{key}: {type(e).__name__}: {e}")
            if key in prev_state:
                new_state[key] = prev_state[key]

    if alerts:
        header = f"🏠 <b>Xior Lutherse Burgwal monitor</b> — {now}\n\n"
        tg_send(header + "\n\n---\n\n".join(alerts))

    if fetch_errors:
        failures["count"] = failures.get("count", 0) + 1
        failures["last_error"] = "; ".join(fetch_errors)
        if failures["count"] == MAX_CONSECUTIVE_FAILURES:
            tg_send(
                f"⚠ Xior monitor: {failures['count']} consecutive failed runs.\n"
                f"Last error: {failures['last_error'][:300]}\n"
                f"Check GitHub Actions logs."
            )
    else:
        failures = {"count": 0, "last_error": ""}

    save_json(STATE_FILE, new_state)
    save_json(FAILURE_FILE, failures)

    print(f"[{now}] urls={len(TARGETS)} errors={len(fetch_errors)} alerts={len(alerts)}")
    for k, v in new_state.items():
        hp = v.get("signals", {}).get("high_priority")
        print(f"  {k}: hash={v.get('hash')} high_prio={hp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
