"""
USD Economic Calendar Generator
Fetches ForexFactory XML feed, filters USD medium + high impact events,
and outputs a subscribable ICS file for Apple Calendar / iOS.
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import uuid
import os

FF_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.xml",
]

IMPACT_FILTER = {"High", "Medium"}
COUNTRY_FILTER = "USD"

EMOJI = {"High": "🔴", "Medium": "🟡"}


def parse_dt(date_str, time_str):
    """
    Parse ForexFactory date/time strings.
    Date format: MM-DD-YYYY
    Time format: H:MMam / H:MMpm  (e.g. '8:30am', '2:00pm')
    Returns (datetime in UTC, is_all_day)
    FF times are US Eastern — we shift by +5h (EST) as a safe default.
    For DST accuracy the offset is kept simple; adjust EDT offset (-4h) if needed.
    """
    date_str = date_str.strip()
    time_str = time_str.strip().lower() if time_str else ""

    try:
        date = datetime.strptime(date_str, "%m-%d-%Y")
    except ValueError:
        return None, True

    if not time_str or time_str in ("", "tentative", "all day", "tbd"):
        return date.replace(hour=0, minute=0), True

    try:
        # Handle formats like "8:30am" or "12:00pm"
        t = datetime.strptime(time_str, "%I:%M%p")
    except ValueError:
        try:
            t = datetime.strptime(time_str, "%H:%M")
        except ValueError:
            return date.replace(hour=0, minute=0), True

    eastern_dt = date.replace(hour=t.hour, minute=t.minute, second=0)
    # Convert Eastern → UTC (use +5h offset; covers EST; close enough for calendar alerts)
    utc_dt = eastern_dt + timedelta(hours=5)
    return utc_dt, False


def fold(text, limit=75):
    """Fold long ICS lines per RFC 5545."""
    text = str(text)
    if len(text) <= limit:
        return text
    lines = []
    while len(text) > limit:
        lines.append(text[:limit])
        text = " " + text[limit:]
    lines.append(text)
    return "\r\n".join(lines)


def fetch_events():
    events = []
    seen = set()

    for url in FF_URLS:
        try:
            resp = requests.get(url, timeout=15,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        except Exception as e:
            print(f"  ⚠️  Could not fetch {url}: {e}")
            continue

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            print(f"  ⚠️  XML parse error for {url}: {e}")
            continue

        for event in root.findall("event"):
            country = (event.findtext("country") or "").strip()
            impact  = (event.findtext("impact")  or "").strip()

            if country != COUNTRY_FILTER:
                continue
            if impact not in IMPACT_FILTER:
                continue

            title    = (event.findtext("title")    or "").strip()
            date_str = (event.findtext("date")     or "").strip()
            time_str = (event.findtext("time")     or "").strip()
            forecast = (event.findtext("forecast") or "").strip()
            previous = (event.findtext("previous") or "").strip()

            dt_utc, all_day = parse_dt(date_str, time_str)
            if dt_utc is None:
                continue

            key = (title, date_str, time_str)
            if key in seen:
                continue
            seen.add(key)

            events.append({
                "title":    title,
                "impact":   impact,
                "dt_utc":   dt_utc,
                "all_day":  all_day,
                "forecast": forecast,
                "previous": previous,
            })

    events.sort(key=lambda e: e["dt_utc"])
    return events


def build_ics(events):
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//USD Economic Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:📊 USD Economic Calendar",
        "X-WR-CALDESC:USA medium and high impact economic events (auto-updated every Sunday)",
        "X-WR-TIMEZONE:America/New_York",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
    ]

    for e in events:
        emoji = EMOJI.get(e["impact"], "")
        summary = f"{emoji} {e['title']} ({e['impact']})"

        desc_parts = []
        if e["forecast"]:
            desc_parts.append(f"Forecast: {e['forecast']}")
        if e["previous"]:
            desc_parts.append(f"Previous: {e['previous']}")
        description = " | ".join(desc_parts) if desc_parts else "No forecast available"

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uuid.uuid4()}@usd-econ-cal")
        lines.append(f"DTSTAMP:{now_stamp}")

        if e["all_day"]:
            lines.append(f"DTSTART;VALUE=DATE:{e['dt_utc'].strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(e['dt_utc'] + timedelta(days=1)).strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{e['dt_utc'].strftime('%Y%m%dT%H%M%SZ')}")
            lines.append(f"DTEND:{(e['dt_utc'] + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}")

        lines.append(fold(f"SUMMARY:{summary}"))
        lines.append(fold(f"DESCRIPTION:{description}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


if __name__ == "__main__":
    print("📡 Fetching ForexFactory USD events...")
    events = fetch_events()
    print(f"✅ Found {len(events)} USD medium/high impact events")

    ics_content = build_ics(events)

    os.makedirs("docs", exist_ok=True)
    output_path = "docs/calendar.ics"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(ics_content)

    print(f"📅 Written to {output_path}")

    # Summary
    for e in events:
        tag = EMOJI.get(e["impact"], "")
        print(f"  {tag} {e['dt_utc'].strftime('%a %b %d %H:%M UTC')}  {e['title']}")
