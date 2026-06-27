"""
USD Economic Calendar — Full Year Auto-Sync
Sources:
  1. ForexFactory     — rolling 2-week detailed feed
  2. Federal Reserve  — FOMC meeting dates (full year, deduplicated)
  3. BLS              — CPI, NFP, PPI, Retail Sales etc (full year)
  4. BEA              — GDP, PCE, Trade Balance etc via official ICS feed
"""

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import uuid, json, os, re

EASTERN      = ZoneInfo("America/New_York")
UTC          = timezone.utc
YEAR         = datetime.now().year
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡"}
CACHE_FILE   = "docs/events_cache.json"
ICS_FILE     = "docs/calendar.ics"
HEADERS      = {"User-Agent": "Mozilla/5.0 (EconCalBot/2.0)"}


# ── helpers ───────────────────────────────────────────────────────────────────

def to_utc(dt_naive_eastern):
    return dt_naive_eastern.replace(tzinfo=EASTERN).astimezone(UTC)

def make_event(title, impact, dt_utc, all_day=False,
               forecast="", previous="", source=""):
    return {"title": title, "impact": impact,
            "dt_utc": dt_utc.isoformat(), "all_day": all_day,
            "forecast": forecast, "previous": previous,
            "source": source, "uid": str(uuid.uuid4())}

def parse_date(text):
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y",
                "%m/%d/%Y", "%Y-%m-%d", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


# ── SOURCE 1: ForexFactory XML ────────────────────────────────────────────────

def fetch_forexfactory():
    out = {}
    for url in ["https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
                "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"]:
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"  ⚠️  FF: {e}"); continue

        for ev in root.findall("event"):
            if (ev.findtext("country") or "").strip() != "USD":
                continue
            impact = (ev.findtext("impact") or "").strip()
            if impact not in ("High", "Medium"):
                continue

            title    = (ev.findtext("title")    or "").strip()
            date_s   = (ev.findtext("date")     or "").strip()
            time_s   = (ev.findtext("time")     or "").strip().lower()
            forecast = (ev.findtext("forecast") or "").strip()
            previous = (ev.findtext("previous") or "").strip()

            try:
                date = datetime.strptime(date_s, "%m-%d-%Y")
            except ValueError:
                continue

            if not time_s or time_s in ("tentative", "all day", "tbd"):
                dt_utc, all_day = date.replace(tzinfo=UTC), True
            else:
                t = None
                for fmt in ("%I:%M%p", "%H:%M"):
                    try: t = datetime.strptime(time_s, fmt); break
                    except ValueError: pass
                if t is None:
                    dt_utc, all_day = date.replace(tzinfo=UTC), True
                else:
                    dt_utc  = to_utc(date.replace(hour=t.hour, minute=t.minute))
                    all_day = False

            key = f"FF|{date_s}|{time_s}|{title}"
            out[key] = make_event(title, impact, dt_utc, all_day,
                                  forecast, previous, "ForexFactory")

    print(f"  ✅ ForexFactory: {len(out)} events")
    return out


# ── SOURCE 2: Federal Reserve FOMC ───────────────────────────────────────────

def fetch_fomc():
    """
    Scrape the Fed page and find FOMC meeting RANGES (e.g. 'January 28-29').
    Each range = one meeting. We take the LAST day as the decision day.
    Strictly deduplicated: one entry per meeting.
    """
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ⚠️  FOMC: {e}"); return {}

    months = (r"January|February|March|April|May|June|"
              r"July|August|September|October|November|December")

    # Only match RANGES (month day1-day2) — ignores single days to avoid dups
    range_pat = re.compile(
        rf"({months})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})",
        re.IGNORECASE
    )

    text     = soup.get_text(" ")
    year_s   = str(YEAR)
    # Find the section for the current year only
    idx      = text.find(year_s)
    if idx == -1:
        print("  ⚠️  FOMC: year section not found"); return {}
    # Limit scan to ~5000 chars so we don't spill into next year
    section  = text[idx: idx + 5000]

    seen_months = set()   # one entry per (Month, Year)
    out = {}

    for m in range_pat.finditer(section):
        month_name = m.group(1).capitalize()
        day2       = int(m.group(3))   # last day of meeting = decision day

        # Skip if we already have this month (handles "July 29-30, August 21-22" etc.)
        if month_name in seen_months:
            continue
        seen_months.add(month_name)

        try:
            dt_naive = datetime.strptime(
                f"{month_name} {day2} {year_s}", "%B %d %Y"
            )
        except ValueError:
            continue

        dt_naive = dt_naive.replace(hour=14, minute=0)  # 2 PM ET announcement
        dt_utc   = to_utc(dt_naive)
        if dt_utc.year != YEAR:
            continue

        key = f"FOMC|{dt_utc.date()}"
        out[key] = make_event("🏛 FOMC Rate Decision", "High",
                              dt_utc, source="Federal Reserve")

    print(f"  ✅ FOMC: {len(out)} events")
    return out


# ── SOURCE 3: BLS Release Schedule ───────────────────────────────────────────

BLS_MAP = {
    "Consumer Price Index":   ("CPI",               "High"),
    "Employment Situation":   ("Non-Farm Payrolls", "High"),
    "Producer Price Index":   ("PPI",               "Medium"),
    "Unemployment Insurance": ("Jobless Claims",    "Medium"),
    "Job Openings":           ("JOLTS",             "Medium"),
    "Advance Monthly":        ("Retail Sales",      "High"),
    "Retail Sales":           ("Retail Sales",      "High"),
    "Import and Export":      ("Import/Export Prices", "Medium"),
    "Productivity":           ("Productivity",      "Medium"),
}

def fetch_bls():
    out = {}
    url = f"https://www.bls.gov/schedule/{YEAR}/home.htm"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ⚠️  BLS: {e}"); return out

    date_pat = re.compile(
        r"(\w+\.?\s+\d{1,2},?\s*\d{4})"
        r"|(\d{1,2}/\d{1,2}/\d{4})"
        r"|(\d{4}-\d{2}-\d{2})",
        re.I
    )

    for tag in soup.find_all(["a", "li", "td", "p"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        title, impact = None, "Medium"
        for kw, (t, i) in BLS_MAP.items():
            if kw.lower() in text.lower():
                title, impact = t, i; break
        if not title:
            continue
        context = text
        if tag.parent:
            context = tag.parent.get_text(" ", strip=True)
        dm = date_pat.search(context)
        if not dm:
            continue
        dt_naive = parse_date(dm.group(0))
        if not dt_naive or dt_naive.year != YEAR:
            continue
        dt_utc = to_utc(dt_naive.replace(hour=8, minute=30))
        key = f"BLS|{dt_utc.date()}|{title}"
        if key not in out:
            out[key] = make_event(title, impact, dt_utc, source="BLS")

    # Fallback: scan raw lines
    if not out:
        lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
        for i, line in enumerate(lines):
            title, impact = None, "Medium"
            for kw, (t, imp) in BLS_MAP.items():
                if kw.lower() in line.lower():
                    title, impact = t, imp; break
            if not title:
                continue
            context = " ".join(lines[max(0, i-3): i+4])
            dm = date_pat.search(context)
            if not dm:
                continue
            dt_naive = parse_date(dm.group(0))
            if not dt_naive or dt_naive.year != YEAR:
                continue
            dt_utc = to_utc(dt_naive.replace(hour=8, minute=30))
            key = f"BLS|{dt_utc.date()}|{title}"
            if key not in out:
                out[key] = make_event(title, impact, dt_utc, source="BLS")

    print(f"  ✅ BLS: {len(out)} events")
    return out


# ── SOURCE 4: BEA — official ICS feed ────────────────────────────────────────

BEA_KEYWORDS = {
    "gross domestic product": ("GDP",                  "High"),
    " gdp":                   ("GDP",                  "High"),
    "personal income":        ("PCE / Personal Income","High"),
    "personal consumption":   ("PCE",                  "High"),
    "international trade":    ("Trade Balance",        "Medium"),
    "current account":        ("Current Account",      "Medium"),
    "corporate profits":      ("Corporate Profits",    "Medium"),
    "gross domestic income":  ("GDI",                  "Medium"),
}

def fetch_bea():
    """Parse BEA's official ICS calendar feed directly."""
    out = {}
    url = "https://www.bea.gov/news/schedule/icalendar"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        raw = r.text
    except Exception as e:
        print(f"  ⚠️  BEA ICS: {e}"); return out

    # Parse ICS manually (simple VEVENT blocks)
    events_raw = raw.split("BEGIN:VEVENT")
    for block in events_raw[1:]:
        # Extract SUMMARY
        sm = re.search(r"SUMMARY[^:]*:(.+)", block)
        if not sm:
            continue
        summary = sm.group(1).strip()

        # Match to our keywords
        title, impact = None, "Medium"
        for kw, (t, i) in BEA_KEYWORDS.items():
            if kw.lower() in summary.lower():
                title, impact = t, i; break
        if not title:
            continue

        # Extract DTSTART
        dm = re.search(r"DTSTART[^:]*:(\d{8})", block)
        if not dm:
            continue
        try:
            dt_naive = datetime.strptime(dm.group(1), "%Y%m%d")
        except ValueError:
            continue

        if dt_naive.year != YEAR:
            continue

        # BEA releases at 8:30 AM ET
        dt_utc = to_utc(dt_naive.replace(hour=8, minute=30))
        key = f"BEA|{dt_utc.date()}|{title}"
        if key not in out:
            out[key] = make_event(title, impact, dt_utc, source="BEA")

    print(f"  ✅ BEA: {len(out)} events")
    return out


# ── ICS builder ───────────────────────────────────────────────────────────────

def fold(line, limit=75):
    if len(line) <= limit: return line
    parts = []
    while len(line) > limit:
        parts.append(line[:limit]); line = " " + line[limit:]
    parts.append(line)
    return "\r\n".join(parts)

def build_ics(events):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//USD Economic Calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             "X-WR-CALNAME:📊 USD Economic Calendar",
             "X-WR-CALDESC:USA medium+high impact — BLS, Fed, BEA, ForexFactory",
             "REFRESH-INTERVAL;VALUE=DURATION:P1D",
             "X-PUBLISHED-TTL:P1D"]

    for e in sorted(events.values(), key=lambda x: x["dt_utc"]):
        emoji = IMPACT_EMOJI.get(e["impact"], "")
        dt    = datetime.fromisoformat(e["dt_utc"])
        end   = dt + timedelta(hours=1)
        desc  = " | ".join(filter(None, [
            f"Forecast: {e['forecast']}" if e.get("forecast") else "",
            f"Previous: {e['previous']}" if e.get("previous") else "",
            f"Source: {e.get('source','')}"
        ])) or "No forecast yet"

        lines += ["BEGIN:VEVENT",
                  f"UID:{e['uid']}@usd-econ-cal",
                  f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            lines += [f"DTSTART;VALUE=DATE:{dt.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"]
        else:
            lines += [f"DTSTART:{dt.strftime('%Y%m%dT%H%M%SZ')}",
                      f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}"]
        lines += [fold(f"SUMMARY:{emoji} {e['title']}"),
                  fold(f"DESCRIPTION:{desc}"),
                  "END:VEVENT"]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f: return json.load(f)
    except Exception: return {}

def prune_old(events):
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    return {k: v for k, v in events.items() if v["dt_utc"] >= cutoff}


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs("docs", exist_ok=True)

    cached = load_cache()
    print(f"📂 Cache: {len(cached)} events\n📡 Fetching all sources...")

    fresh = {}
    fresh.update(fetch_forexfactory())
    fresh.update(fetch_fomc())
    fresh.update(fetch_bls())
    fresh.update(fetch_bea())

    merged = prune_old({**cached, **fresh})
    print(f"\n✅ Total: {len(merged)} events")

    json.dump(merged, open(CACHE_FILE, "w"), indent=2)
    with open(ICS_FILE, "w", encoding="utf-8") as f:
        f.write(build_ics(merged))
    print(f"📅 Written → {ICS_FILE}")

    SGT = ZoneInfo("Asia/Singapore")
    now = datetime.now(UTC).isoformat()
    upcoming = sorted([e for e in merged.values() if e["dt_utc"] >= now],
                      key=lambda x: x["dt_utc"])[:50]
    print("\n── Upcoming (SGT) ──")
    for e in upcoming:
        sgt = datetime.fromisoformat(e["dt_utc"]).astimezone(SGT)
        print(f"  {IMPACT_EMOJI.get(e['impact'],'')} "
              f"{sgt.strftime('%d %b %H:%M')} SGT  "
              f"{e['title']}  [{e.get('source','')}]")
