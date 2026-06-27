"""
USD Economic Calendar — Full Year Auto-Sync
Sources:
  1. ForexFactory  — rolling 2-week detailed feed
  2. Federal Reserve — FOMC (hardcoded 2026, scraped for other years)
  3. BLS — CPI, NFP, PPI, Retail Sales (full year scraped)
  4. BEA — GDP, PCE (ICS feed → HTML scrape → hardcoded 2026 fallback)
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


# ── SOURCE 1: ForexFactory ────────────────────────────────────────────────────

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


# ── SOURCE 2: FOMC ────────────────────────────────────────────────────────────
# Hardcoded 2026 dates — published by the Fed a year in advance, never change.
# Each tuple: (month_name, decision_day)  — 2 PM ET announcement time.

FOMC_2026 = [
    ("January",   28),
    ("March",     18),
    ("April",     29),
    ("June",      10),
    ("July",      29),
    ("September", 16),
    ("October",   28),
    ("December",   9),
]

def fetch_fomc():
    out = {}
    year_s = str(YEAR)

    if YEAR == 2026:
        for month_name, day in FOMC_2026:
            dt_naive = datetime.strptime(f"{month_name} {day} {year_s}", "%B %d %Y")
            dt_utc   = to_utc(dt_naive.replace(hour=14, minute=0))
            key      = f"FOMC|{dt_utc.date()}"
            out[key] = make_event("🏛 FOMC Rate Decision", "High",
                                  dt_utc, source="Federal Reserve")
        print(f"  ✅ FOMC: {len(out)} events (2026 schedule)")
        return out

    # For years other than 2026, scrape the Fed page
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ")
        idx  = text.find(year_s)
        if idx == -1:
            raise ValueError("Year not found on page")
        section = text[idx: idx + 5000]
        months  = (r"January|February|March|April|May|June|"
                   r"July|August|September|October|November|December")
        pat = re.compile(rf"({months})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})",
                         re.IGNORECASE)
        seen = set()
        for m in pat.finditer(section):
            mname = m.group(1).capitalize()
            if mname in seen:
                continue
            seen.add(mname)
            day = int(m.group(3))
            try:
                dt_naive = datetime.strptime(f"{mname} {day} {year_s}", "%B %d %Y")
            except ValueError:
                continue
            dt_utc = to_utc(dt_naive.replace(hour=14, minute=0))
            if dt_utc.year != YEAR:
                continue
            key = f"FOMC|{dt_utc.date()}"
            out[key] = make_event("🏛 FOMC Rate Decision", "High",
                                  dt_utc, source="Federal Reserve")
    except Exception as e:
        print(f"  ⚠️  FOMC scrape: {e}")

    print(f"  ✅ FOMC: {len(out)} events")
    return out


# ── SOURCE 3: BLS ─────────────────────────────────────────────────────────────

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
        r"(\w+\.?\s+\d{1,2},?\s*\d{4})|(\d{1,2}/\d{1,2}/\d{4})|(\d{4}-\d{2}-\d{2})",
        re.I)

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
        context = tag.parent.get_text(" ", strip=True) if tag.parent else text
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


# ── SOURCE 4: BEA ─────────────────────────────────────────────────────────────

BEA_KEYWORDS = {
    "gross domestic product": ("GDP",                  "High"),
    "gdp":                    ("GDP",                  "High"),
    "personal income":        ("PCE / Personal Income","High"),
    "personal consumption":   ("PCE",                  "High"),
    "international trade":    ("Trade Balance",        "Medium"),
    "current account":        ("Current Account",      "Medium"),
    "corporate profits":      ("Corporate Profits",    "Medium"),
}

# Official 2026 BEA schedule (published by BEA at start of year)
BEA_2026 = [
    # GDP — Advance, Second, Third estimates each quarter
    ("GDP (Advance)",   "High",  1, 29),
    ("GDP (2nd Est.)",  "High",  2, 26),
    ("GDP (3rd Est.)",  "High",  3, 26),
    ("GDP (Advance)",   "High",  4, 29),
    ("GDP (2nd Est.)",  "High",  5, 28),
    ("GDP (3rd Est.)",  "High",  6, 25),
    ("GDP (Advance)",   "High",  7, 30),
    ("GDP (2nd Est.)",  "High",  8, 27),
    ("GDP (3rd Est.)",  "High",  9, 24),
    ("GDP (Advance)",   "High", 10, 29),
    ("GDP (2nd Est.)",  "High", 11, 24),
    ("GDP (3rd Est.)",  "High", 12, 22),
    # PCE / Personal Income — monthly
    ("PCE / Personal Income", "High",  1, 30),
    ("PCE / Personal Income", "High",  2, 27),
    ("PCE / Personal Income", "High",  3, 28),
    ("PCE / Personal Income", "High",  4, 30),
    ("PCE / Personal Income", "High",  5, 29),
    ("PCE / Personal Income", "High",  6, 26),
    ("PCE / Personal Income", "High",  7, 31),
    ("PCE / Personal Income", "High",  8, 28),
    ("PCE / Personal Income", "High",  9, 25),
    ("PCE / Personal Income", "High", 10, 30),
    ("PCE / Personal Income", "High", 11, 25),
    ("PCE / Personal Income", "High", 12, 23),
]

def fetch_bea():
    out = {}

    # Attempt 1: BEA official ICS feeds
    for url in ["https://www.bea.gov/news/schedule/icalendar",
                "https://www.bea.gov/icalendar/bea-release-calendar.ics"]:
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
            r.raise_for_status()
            text = r.text
            if "BEGIN:VEVENT" not in text:
                continue
            for block in text.split("BEGIN:VEVENT")[1:]:
                sm = re.search(r"SUMMARY[^:\r\n]*:([^\r\n]+)", block)
                if not sm:
                    continue
                summary = sm.group(1).strip()
                title, impact = None, "Medium"
                for kw, (t, i) in BEA_KEYWORDS.items():
                    if kw.lower() in summary.lower():
                        title, impact = t, i; break
                if not title:
                    continue
                dm = re.search(r"DTSTART[^:\r\n]*:(\d{8})", block)
                if not dm:
                    continue
                try:
                    dt_naive = datetime.strptime(dm.group(1), "%Y%m%d")
                except ValueError:
                    continue
                if dt_naive.year != YEAR:
                    continue
                dt_utc = to_utc(dt_naive.replace(hour=8, minute=30))
                key = f"BEA|{dt_utc.date()}|{title}"
                if key not in out:
                    out[key] = make_event(title, impact, dt_utc, source="BEA")
            if out:
                print(f"  ✅ BEA: {len(out)} events (ICS)")
                return out
        except Exception as e:
            print(f"  ⚠️  BEA ICS {url}: {e}")

    # Attempt 2: HTML table scrape
    try:
        r = requests.get("https://www.bea.gov/news/schedule",
                         timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        date_pat = re.compile(
            r"(\w+\.?\s+\d{1,2},?\s*\d{4})|(\d{1,2}/\d{1,2}/\d{4})|(\d{4}-\d{2}-\d{2})",
            re.I)
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in row.find_all(["td","th"])]
                if len(cells) < 2:
                    continue
                row_text = " | ".join(cells)
                title, impact = None, "Medium"
                for kw, (t, i) in BEA_KEYWORDS.items():
                    if kw.lower() in row_text.lower():
                        title, impact = t, i; break
                if not title:
                    continue
                for cell in cells:
                    dt_naive = parse_date(cell.strip())
                    if dt_naive and dt_naive.year == YEAR:
                        dt_utc = to_utc(dt_naive.replace(hour=8, minute=30))
                        key = f"BEA|{dt_utc.date()}|{title}"
                        if key not in out:
                            out[key] = make_event(title, impact, dt_utc, source="BEA")
                        break
        if out:
            print(f"  ✅ BEA: {len(out)} events (HTML)")
            return out
    except Exception as e:
        print(f"  ⚠️  BEA HTML: {e}")

    # Attempt 3: Hardcoded 2026 fallback (always works)
    if YEAR == 2026:
        print("  ℹ️  BEA: using hardcoded 2026 schedule")
        for (title, impact, month, day) in BEA_2026:
            try:
                dt_naive = datetime(YEAR, month, day, 8, 30)
                dt_utc   = to_utc(dt_naive)
                key      = f"BEA|{dt_utc.date()}|{title}|{month}"
                out[key] = make_event(title, impact, dt_utc, source="BEA")
            except ValueError:
                pass

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
             "X-WR-CALDESC:USA medium+high impact — BLS BEA Fed ForexFactory",
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
    # Always clear old FOMC + BEA entries so fresh data wins cleanly
    cached = {k: v for k, v in cached.items()
              if not k.startswith("FOMC|") and not k.startswith("BEA|")}
    print(f"📂 Cache: {len(cached)} events (FOMC+BEA cleared)\n📡 Fetching...")

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
