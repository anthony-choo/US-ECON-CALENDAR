"""
USD Economic Calendar — Full Year Auto-Sync
Sources:
  1. ForexFactory     — rolling 2-week detailed feed
  2. Federal Reserve  — FOMC meeting dates (full year)
  3. BLS              — CPI, NFP, PPI, Retail Sales etc (full year)
  4. BEA              — GDP, PCE, Trade Balance etc (full year)
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


# ── helpers ──────────────────────────────────────────────────────────────────

def to_utc(dt_naive_eastern):
    """Convert naive Eastern datetime → UTC, DST-aware."""
    return dt_naive_eastern.replace(tzinfo=EASTERN).astimezone(UTC)

def make_event(title, impact, dt_utc, all_day=False,
               forecast="", previous="", source=""):
    return {"title": title, "impact": impact,
            "dt_utc": dt_utc.isoformat(), "all_day": all_day,
            "forecast": forecast, "previous": previous,
            "source": source, "uid": str(uuid.uuid4())}


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
            print(f"  ⚠️  FF {url}: {e}"); continue

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


# ── SOURCE 2: Federal Reserve — FOMC ─────────────────────────────────────────

def fetch_fomc():
    out = {}
    url = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ⚠️  FOMC: {e}"); return out

    year_s = str(YEAR)
    full_text = soup.get_text(" ")

    # Pattern: "January 27-28" or "March 18" possibly followed by year
    months_re = (r"(January|February|March|April|May|June|"
                 r"July|August|September|October|November|December)")
    pat = rf"{months_re}\s+(\d{{1,2}})(?:[-–](\d{{1,2}}))?(?:,?\s*{year_s})?"

    # Only scan the section near the current year
    idx = full_text.find(year_s)
    if idx == -1:
        print("  ⚠️  FOMC: year not found on page"); return out
    section = full_text[idx: idx + 3000]

    for m in re.finditer(pat, section):
        month_name, day1, day2 = m.group(1), m.group(2), m.group(3)
        day = int(day2) if day2 else int(day1)   # decision = last day of meeting
        try:
            dt_naive = datetime.strptime(f"{month_name} {day} {year_s}", "%B %d %Y")
        except ValueError:
            continue
        dt_naive = dt_naive.replace(hour=14, minute=0)   # Fed announces at 2 PM ET
        dt_utc   = to_utc(dt_naive)
        if dt_utc.year != YEAR:
            continue
        key = f"FOMC|{dt_utc.date()}"
        if key not in out:
            out[key] = make_event("FOMC Rate Decision", "High",
                                  dt_utc, source="Federal Reserve")

    print(f"  ✅ FOMC: {len(out)} events")
    return out


# ── SOURCE 3: BLS Release Schedule ───────────────────────────────────────────

BLS_MAP = {
    "Consumer Price Index":   ("CPI",              "High"),
    "Employment Situation":   ("Non-Farm Payrolls","High"),
    "Producer Price Index":   ("PPI",              "Medium"),
    "Unemployment Insurance": ("Jobless Claims",   "Medium"),
    "Job Openings":           ("JOLTS",            "Medium"),
    "Advance Monthly Retail": ("Retail Sales",     "High"),
    "Import and Export":      ("Import/Export Prices","Medium"),
    "Productivity and Costs": ("Productivity",     "Medium"),
    "Consumer Expenditures":  ("Consumer Spending","Medium"),
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

    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 2:
            continue
        row_text = " ".join(cells)

        title, impact = None, "Medium"
        for kw, (t, i) in BLS_MAP.items():
            if kw.lower() in row_text.lower():
                title, impact = t, i; break
        if not title:
            continue

        # Find date in any cell
        dt_naive = None
        for cell in cells:
            for fmt in ("%B %d, %Y", "%b. %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    dt_naive = datetime.strptime(cell.strip(), fmt); break
                except ValueError: pass
            if dt_naive: break
        if not dt_naive or dt_naive.year != YEAR:
            continue

        # Time — BLS typically 8:30 AM ET
        tm = re.search(r"(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?", row_text, re.I)
        h, mn = (int(tm.group(1)), int(tm.group(2))) if tm else (8, 30)
        if tm and tm.group(3) and "p" in tm.group(3).lower() and h != 12:
            h += 12

        dt_utc = to_utc(dt_naive.replace(hour=h, minute=mn))
        key = f"BLS|{dt_utc.date()}|{title}"
        if key not in out:
            out[key] = make_event(title, impact, dt_utc, source="BLS")

    print(f"  ✅ BLS: {len(out)} events")
    return out


# ── SOURCE 4: BEA Release Schedule ───────────────────────────────────────────

BEA_MAP = {
    "Gross Domestic Product":  ("GDP",                "High"),
    " GDP":                    ("GDP",                "High"),
    "Personal Income":         ("PCE / Personal Income","High"),
    "Personal Consumption":    ("PCE",                "High"),
    "International Trade":     ("Trade Balance",      "Medium"),
    "Current Account":         ("Current Account",    "Medium"),
    "Corporate Profits":       ("Corporate Profits",  "Medium"),
    "Gross Domestic Income":   ("GDI",               "Medium"),
}

def fetch_bea():
    out = {}
    url = "https://www.bea.gov/news/schedule"
    try:
        r = requests.get(url, timeout=15, headers=HEADERS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ⚠️  BEA: {e}"); return out

    date_pat = (r"(\w+ \d{1,2},\s*\d{4})"
                r"|(\d{1,2}/\d{1,2}/\d{4})"
                r"|(\d{4}-\d{2}-\d{2})")

    for row in soup.find_all("tr"):
        row_text = row.get_text(" ", strip=True)
        title, impact = None, "Medium"
        for kw, (t, i) in BEA_MAP.items():
            if kw.lower() in row_text.lower():
                title, impact = t, i; break
        if not title:
            continue

        dm = re.search(date_pat, row_text)
        if not dm:
            continue
        date_s = dm.group(0).strip()
        dt_naive = None
        for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%Y-%m-%d"):
            try: dt_naive = datetime.strptime(date_s, fmt); break
            except ValueError: pass
        if not dt_naive or dt_naive.year != YEAR:
            continue

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
            f"Source: {e.get('source','')}"]))  or "No forecast yet"

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
                      key=lambda x: x["dt_utc"])[:30]
    print("\n── Upcoming (SGT) ──")
    for e in upcoming:
        sgt = datetime.fromisoformat(e["dt_utc"]).astimezone(SGT)
        src = e.get("source", "")
        print(f"  {IMPACT_EMOJI.get(e['impact'],'')} "
              f"{sgt.strftime('%d %b %H:%M')} SGT  "
              f"{e['title']}  [{src}]")
