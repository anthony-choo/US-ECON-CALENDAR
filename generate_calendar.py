"""
USD + KRW Economic Calendar — Full Year Auto-Sync
US:  ForexFactory · FOMC · BLS · BEA · ISM · Conference Board · U of Michigan
KR:  ForexFactory KRW · BOK Rate Decisions · Samsung / SK Hynix / LG Energy / Hyundai Earnings
"""

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import uuid, json, os, re
import calendar as cal_mod

EASTERN = ZoneInfo("America/New_York")
KST     = ZoneInfo("Asia/Seoul")
SGT     = ZoneInfo("Asia/Singapore")
UTC     = timezone.utc
YEAR    = datetime.now().year

IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡"}
CACHE_FILE   = "docs/events_cache.json"
ICS_FILE     = "docs/calendar.ics"
HEADERS      = {"User-Agent": "Mozilla/5.0 (EconCalBot/2.0)"}
YF_HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Every key with these prefixes is cleared from cache and rebuilt fresh on each run
ALWAYS_REFRESH = ("FOMC|", "BOK|", "ISM|", "CB|", "UOM|", "BEA|", "FF|", "EARN|")


# ── helpers ───────────────────────────────────────────────────────────────────

def to_utc_et(dt):  return dt.replace(tzinfo=EASTERN).astimezone(UTC)
def to_utc_kst(dt): return dt.replace(tzinfo=KST).astimezone(UTC)

def nth_weekday(year, month, weekday, n):
    """nth occurrence of weekday (0=Mon … 6=Sun) in the month."""
    count = 0
    for d in range(1, 32):
        try:
            day = datetime(year, month, d)
            if day.weekday() == weekday:
                count += 1
                if count == n:
                    return day
        except ValueError:
            break
    return None

def last_weekday(year, month, weekday):
    """Last occurrence of weekday in the month."""
    for d in range(cal_mod.monthrange(year, month)[1], 0, -1):
        day = datetime(year, month, d)
        if day.weekday() == weekday:
            return day
    return None

def nth_business_day(year, month, n, holidays=frozenset()):
    """nth Mon–Fri that is not a US federal holiday."""
    count = 0
    for d in range(1, 32):
        try:
            day = datetime(year, month, d)
            if day.weekday() < 5 and day.date() not in holidays:
                count += 1
                if count == n:
                    return day
        except ValueError:
            break
    return None

def us_holidays(year):
    """Set of US federal holiday dates for the given year."""
    h = set()
    h.add(datetime(year, 1,  1).date())                          # New Year's Day
    d = nth_weekday(year, 1, 0, 3);  h.add(d.date()) if d else None  # MLK Day (3rd Mon Jan)
    d = nth_weekday(year, 2, 0, 3);  h.add(d.date()) if d else None  # Presidents' Day
    d = last_weekday(year, 5, 0);    h.add(d.date()) if d else None  # Memorial Day
    h.add(datetime(year, 6, 19).date())                          # Juneteenth
    jul4 = datetime(year, 7, 4)                                  # Independence Day
    if   jul4.weekday() == 5: h.add(datetime(year, 7, 3).date())   # Sat → Fri observed
    elif jul4.weekday() == 6: h.add(datetime(year, 7, 5).date())   # Sun → Mon observed
    else:                     h.add(jul4.date())
    d = nth_weekday(year, 9,  0, 1); h.add(d.date()) if d else None  # Labor Day
    d = nth_weekday(year, 10, 0, 2); h.add(d.date()) if d else None  # Columbus Day
    h.add(datetime(year, 11, 11).date())                         # Veterans Day
    d = nth_weekday(year, 11, 3, 4); h.add(d.date()) if d else None  # Thanksgiving
    h.add(datetime(year, 12, 25).date())                         # Christmas
    return h

def make_event(title, impact, dt_utc, all_day=False,
               forecast="", previous="", source="", country="US"):
    return {"title": title, "impact": impact, "dt_utc": dt_utc.isoformat(),
            "all_day": all_day, "forecast": forecast, "previous": previous,
            "source": source, "country": country, "uid": str(uuid.uuid4())}

def parse_date(text):
    for fmt in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y",
                "%m/%d/%Y", "%Y-%m-%d", "%B %d %Y"):
        try: return datetime.strptime(text.strip(), fmt)
        except ValueError: pass
    return None

def normalize(t):
    """Strip emojis/punct, lowercase, collapse spaces — used for dedup keys."""
    t = re.sub(r'[^\w\s]', '', t.encode('ascii', 'ignore').decode())
    return ' '.join(t.lower().split())


# ── deduplication ─────────────────────────────────────────────────────────────

# Higher number = wins when two events are detected as duplicates
SRC_PRIORITY = {
    "ForexFactory":     3,
    "BLS":              2, "BEA":          2, "Federal Reserve": 2,
    "Bank of Korea":    2, "Yahoo Finance": 2,
    "ISM":              1, "Conference Board": 1, "U of Michigan": 1,
    "Calculated (approx)": 0,
}

STOPWORDS = {"the", "and", "for", "from", "rate", "change", "index",
             "price", "prices", "data", "monthly", "annual", "report",
             "survey", "estimate", "preliminary", "results"}

def keywords(title):
    return set(normalize(title).split()) - STOPWORDS

def deduplicate(events):
    """
    Remove events that are near-duplicates:
    same date window (≤90 min apart) AND share ≥1 significant keyword.
    Lower-priority source is dropped.
    """
    items  = list(events.items())
    remove = set()

    for i in range(len(items)):
        if items[i][0] in remove: continue
        k1, e1 = items[i]
        dt1 = datetime.fromisoformat(e1["dt_utc"])
        kw1 = keywords(e1["title"])
        p1  = SRC_PRIORITY.get(e1.get("source", ""), 0)

        for j in range(i + 1, len(items)):
            if items[j][0] in remove: continue
            k2, e2 = items[j]
            dt2 = datetime.fromisoformat(e2["dt_utc"])

            if abs((dt1 - dt2).total_seconds()) > 5400:   # > 90 min → different events
                continue
            if not kw1.intersection(keywords(e2["title"])): # no shared keyword → different
                continue

            # Duplicate — keep higher priority
            p2 = SRC_PRIORITY.get(e2.get("source", ""), 0)
            if p1 >= p2:
                remove.add(k2)
            else:
                remove.add(k1); break

    cleaned = {k: v for k, v in events.items() if k not in remove}
    if remove:
        print(f"  🧹 Deduplication removed {len(remove)} entries → {len(cleaned)} unique events")
    return cleaned


# ── SOURCE 1: ForexFactory (USD + KRW) ───────────────────────────────────────
# NOTE: FF XML uses Eastern Time for ALL currencies (including KRW).
# So we always convert with to_utc_et, regardless of currency.

def fetch_forexfactory():
    out = {}
    for url in ["https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
                "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"]:
        try:
            r = requests.get(url, timeout=15, headers=HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"  ⚠️  FF ({url[-20:]}): {e}"); continue

        for ev in root.findall("event"):
            cc = (ev.findtext("country") or "").strip()
            if cc not in ("USD", "KRW"): continue
            impact = (ev.findtext("impact") or "").strip()
            if impact not in ("High", "Medium"): continue

            title    = (ev.findtext("title")    or "").strip()
            date_s   = (ev.findtext("date")     or "").strip()
            time_s   = (ev.findtext("time")     or "").strip().lower()
            forecast = (ev.findtext("forecast") or "").strip()
            previous = (ev.findtext("previous") or "").strip()

            try: date = datetime.strptime(date_s, "%m-%d-%Y")
            except ValueError: continue

            if not time_s or time_s in ("tentative", "all day", "tbd"):
                dt_utc, all_day, t_key = date.replace(tzinfo=UTC), True, "allday"
            else:
                t = None
                for fmt in ("%I:%M%p", "%H:%M"):
                    try: t = datetime.strptime(time_s, fmt); break
                    except ValueError: pass
                if t is None:
                    dt_utc, all_day, t_key = date.replace(tzinfo=UTC), True, "allday"
                else:
                    # FF uses ET for all currencies
                    dt_utc  = to_utc_et(date.replace(hour=t.hour, minute=t.minute))
                    all_day = False
                    t_key   = f"{t.hour:02d}{t.minute:02d}"

            flag = "🇰🇷 " if cc == "KRW" else ""
            ctry = "KR"  if cc == "KRW" else "US"
            # Normalised key deduplicates events across thisweek/nextweek feeds
            key  = f"FF|{cc}|{date_s}|{t_key}|{normalize(title)}"
            out[key] = make_event(f"{flag}{title}", impact, dt_utc, all_day,
                                  forecast, previous, "ForexFactory", ctry)

    us = sum(1 for k in out if "|USD|" in k)
    kr = sum(1 for k in out if "|KRW|" in k)
    print(f"  ✅ ForexFactory: {us} USD + {kr} KRW events")
    return out


# ── SOURCE 2: FOMC (hardcoded 2026, scraped otherwise) ───────────────────────
# Decision announced at 14:00 ET. 8 meetings per year.

FOMC_2026 = [
    ("January",   28), ("March",    18), ("April",     29), ("June",     10),
    ("July",      29), ("September",16), ("October",   28), ("December",  9),
]

def fetch_fomc():
    out = {}
    if YEAR == 2026:
        for month, day in FOMC_2026:
            dt = to_utc_et(datetime.strptime(f"{month} {day} 2026", "%B %d %Y")
                           .replace(hour=14, minute=0))
            out[f"FOMC|{dt.date()}"] = make_event(
                "🏛 FOMC Rate Decision", "High", dt, source="Federal Reserve")
        print(f"  ✅ FOMC: {len(out)} events"); return out

    # Scrape for other years
    try:
        r    = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                            timeout=15, headers=HEADERS)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ")
        idx  = text.find(str(YEAR))
        if idx == -1: raise ValueError("year not found on page")
        months = (r"January|February|March|April|May|June|"
                  r"July|August|September|October|November|December")
        seen = set()
        for m in re.compile(rf"({months})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})",
                            re.I).finditer(text[idx: idx + 5000]):
            mn = m.group(1).capitalize()
            if mn in seen: continue
            seen.add(mn)
            dt_n = datetime.strptime(f"{mn} {int(m.group(3))} {YEAR}",
                                     "%B %d %Y").replace(hour=14, minute=0)
            dt   = to_utc_et(dt_n)
            if dt.year == YEAR:
                out[f"FOMC|{dt.date()}"] = make_event(
                    "🏛 FOMC Rate Decision", "High", dt, source="Federal Reserve")
    except Exception as e:
        print(f"  ⚠️  FOMC scrape: {e}")

    print(f"  ✅ FOMC: {len(out)} events"); return out


# ── SOURCE 3: BLS ─────────────────────────────────────────────────────────────
# Each release has its known ET release time for accuracy.

BLS_MAP = {
    "Consumer Price Index":   ("CPI",                "High",   8, 30),
    "Employment Situation":   ("Non-Farm Payrolls",  "High",   8, 30),
    "Producer Price Index":   ("PPI",                "Medium", 8, 30),
    "Unemployment Insurance": ("Jobless Claims",     "Medium", 8, 30),
    "Job Openings":           ("JOLTS",              "Medium",10,  0),  # JOLTS = 10 AM ET
    "Advance Monthly":        ("Retail Sales",       "High",   8, 30),
    "Retail Sales":           ("Retail Sales",       "High",   8, 30),
    "Import and Export":      ("Import/Export Prices","Medium", 8, 30),
    "Productivity":           ("Productivity",       "Medium", 8, 30),
}

def fetch_bls():
    out = {}
    url = f"https://www.bls.gov/schedule/{YEAR}/home.htm"
    try:
        r    = requests.get(url, timeout=15, headers=HEADERS); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  ⚠️  BLS: {e}"); return out

    dp = re.compile(
        r"(\w+\.?\s+\d{1,2},?\s*\d{4})|(\d{1,2}/\d{1,2}/\d{4})|(\d{4}-\d{2}-\d{2})", re.I)

    for tag in soup.find_all(["a", "li", "td", "p"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 300: continue
        title, impact, rh, rm = None, "Medium", 8, 30
        for kw, (t, i, h, mi) in BLS_MAP.items():
            if kw.lower() in text.lower():
                title, impact, rh, rm = t, i, h, mi; break
        if not title: continue
        ctx = tag.parent.get_text(" ", strip=True) if tag.parent else text
        dm  = dp.search(ctx)
        if not dm: continue
        dt_n = parse_date(dm.group(0))
        if not dt_n or dt_n.year != YEAR: continue
        dt_utc = to_utc_et(dt_n.replace(hour=rh, minute=rm))
        key = f"BLS|{dt_utc.date()}|{title}"
        if key not in out:
            out[key] = make_event(title, impact, dt_utc, source="BLS")

    # Fallback: scan raw lines
    if not out:
        lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
        for i, line in enumerate(lines):
            title, impact, rh, rm = None, "Medium", 8, 30
            for kw, (t, imp, h, mi) in BLS_MAP.items():
                if kw.lower() in line.lower():
                    title, impact, rh, rm = t, imp, h, mi; break
            if not title: continue
            ctx = " ".join(lines[max(0, i-3): i+4])
            dm  = dp.search(ctx)
            if not dm: continue
            dt_n = parse_date(dm.group(0))
            if not dt_n or dt_n.year != YEAR: continue
            dt_utc = to_utc_et(dt_n.replace(hour=rh, minute=rm))
            key = f"BLS|{dt_utc.date()}|{title}"
            if key not in out:
                out[key] = make_event(title, impact, dt_utc, source="BLS")

    print(f"  ✅ BLS: {len(out)} events"); return out


# ── SOURCE 4: BEA (hardcoded 2026, released at 08:30 ET) ─────────────────────

BEA_2026 = [
    ("GDP (Advance)",        "High",  1, 29), ("GDP (2nd Est.)",       "High",  2, 26),
    ("GDP (3rd Est.)",       "High",  3, 26), ("GDP (Advance)",        "High",  4, 29),
    ("GDP (2nd Est.)",       "High",  5, 28), ("GDP (3rd Est.)",       "High",  6, 25),
    ("GDP (Advance)",        "High",  7, 30), ("GDP (2nd Est.)",       "High",  8, 27),
    ("GDP (3rd Est.)",       "High",  9, 24), ("GDP (Advance)",        "High", 10, 29),
    ("GDP (2nd Est.)",       "High", 11, 24), ("GDP (3rd Est.)",       "High", 12, 22),
    ("PCE / Personal Income","High",  1, 30), ("PCE / Personal Income","High",  2, 27),
    ("PCE / Personal Income","High",  3, 28), ("PCE / Personal Income","High",  4, 30),
    ("PCE / Personal Income","High",  5, 29), ("PCE / Personal Income","High",  6, 26),
    ("PCE / Personal Income","High",  7, 31), ("PCE / Personal Income","High",  8, 28),
    ("PCE / Personal Income","High",  9, 25), ("PCE / Personal Income","High", 10, 30),
    ("PCE / Personal Income","High", 11, 25), ("PCE / Personal Income","High", 12, 23),
]

def fetch_bea():
    out = {}
    # Try official ICS feeds first
    for url in ["https://www.bea.gov/news/schedule/icalendar",
                "https://www.bea.gov/icalendar/bea-release-calendar.ics"]:
        try:
            r = requests.get(url, timeout=15, headers=HEADERS); r.raise_for_status()
            if "BEGIN:VEVENT" not in r.text: continue
            kws = {"gross domestic product": ("GDP",                 "High"),
                   "personal income":        ("PCE / Personal Income","High"),
                   "international trade":    ("Trade Balance",       "Medium")}
            for block in r.text.split("BEGIN:VEVENT")[1:]:
                sm = re.search(r"SUMMARY[^:\r\n]*:([^\r\n]+)", block)
                if not sm: continue
                title, impact = None, "Medium"
                for kw, (t, i) in kws.items():
                    if kw in sm.group(1).lower(): title, impact = t, i; break
                if not title: continue
                dm = re.search(r"DTSTART[^:\r\n]*:(\d{8})", block)
                if not dm: continue
                try: dt_n = datetime.strptime(dm.group(1), "%Y%m%d")
                except ValueError: continue
                if dt_n.year != YEAR: continue
                dt_utc = to_utc_et(dt_n.replace(hour=8, minute=30))
                key = f"BEA|{dt_utc.date()}|{title}"
                if key not in out:
                    out[key] = make_event(title, impact, dt_utc, source="BEA")
            if out:
                print(f"  ✅ BEA: {len(out)} events (ICS)"); return out
        except Exception:
            pass

    # Hardcoded 2026 fallback
    if YEAR == 2026:
        for title, impact, month, day in BEA_2026:
            try:
                dt_utc = to_utc_et(datetime(YEAR, month, day, 8, 30))
                key = f"BEA|{dt_utc.date()}|{title}|{month}"
                out[key] = make_event(title, impact, dt_utc, source="BEA")
            except ValueError: pass

    print(f"  ✅ BEA: {len(out)} events"); return out


# ── SOURCE 5: ISM (1st and 3rd business day of month, 10:00 ET) ──────────────

def fetch_ism():
    out  = {}
    hols = us_holidays(YEAR)
    for month in range(1, 13):
        for n, label in [(1, "ISM Manufacturing PMI"), (3, "ISM Services PMI")]:
            d = nth_business_day(YEAR, month, n, hols)
            if d:
                dt = to_utc_et(d.replace(hour=10, minute=0))
                out[f"ISM|{label[:3]}|{dt.date()}"] = make_event(
                    label, "Medium", dt, source="ISM")
    print(f"  ✅ ISM: {len(out)} events"); return out


# ── SOURCE 6: Conference Board Consumer Confidence (last Tuesday, 10:00 ET) ───

def fetch_conference_board():
    out = {}
    for month in range(1, 13):
        d = last_weekday(YEAR, month, 1)   # Tuesday = 1
        if d:
            dt = to_utc_et(d.replace(hour=10, minute=0))
            out[f"CB|{dt.date()}"] = make_event(
                "Consumer Confidence (CB)", "Medium", dt, source="Conference Board")
    print(f"  ✅ Conference Board: {len(out)} events"); return out


# ── SOURCE 7: U of Michigan Sentiment (2nd + 4th Friday, 10:00 ET) ───────────

def fetch_uom():
    out = {}
    for month in range(1, 13):
        for n, label in [(2, "Prelim"), (4, "Final")]:
            d = nth_weekday(YEAR, month, 4, n)   # Friday = 4
            if d:
                dt = to_utc_et(d.replace(hour=10, minute=0))
                out[f"UOM|{label}|{dt.date()}"] = make_event(
                    f"Consumer Sentiment UoM ({label})", "Medium",
                    dt, source="U of Michigan")
    print(f"  ✅ U of Michigan: {len(out)} events"); return out


# ── SOURCE 8: BOK Rate Decisions (3rd Thursday of 8 months, 10:00 KST) ───────

BOK_MONTHS = [1, 2, 4, 5, 7, 8, 10, 11]

def fetch_bok():
    out = {}
    for month in BOK_MONTHS:
        d = nth_weekday(YEAR, month, 3, 3)   # Thursday = 3, 3rd occurrence
        if d:
            dt = to_utc_kst(d.replace(hour=10, minute=0))
            out[f"BOK|{dt.date()}"] = make_event(
                "🇰🇷 BOK Interest Rate Decision", "High",
                dt, source="Bank of Korea", country="KR")
    print(f"  ✅ BOK: {len(out)} events"); return out


# ── SOURCE 9: Korean Company Earnings ────────────────────────────────────────
#
# Companies tracked (biggest movers of KOSPI 200 futures):
#   Samsung Electronics (005930.KS) ~25% of KOSPI weight
#   SK Hynix           (000660.KS) ~7%
#   LG Energy Solution (373220.KS) ~4%
#   Hyundai Motor      (005380.KS) ~3%
#
# Auto-update: Yahoo Finance API (no key needed) fetches confirmed dates daily.
# Hardcoded fallback: approximate dates when Yahoo Finance has not yet announced.
#
# Samsung PRELIMINARY results (잠정실적) are separate from and MORE market-moving
# than full results — always hardcoded since Yahoo Finance doesn't track these.
# They are released ~5th-8th business day of Jan/Apr/Jul/Oct at 08:00 KST.

KOSPI_COMPANIES = {
    "005930.KS": ("Samsung Electronics", "High"),
    "000660.KS": ("SK Hynix",            "High"),
    "373220.KS": ("LG Energy Solution",  "High"),
    "005380.KS": ("Hyundai Motor",       "High"),
}

# (month, approx_day, hour_kst) — used when Yahoo Finance hasn't announced yet
EARNINGS_FALLBACK = {
    "005930.KS": [(1,29,16),(4,29,16),(7,29,16),(10,29,16)],  # full results ~4th week
    "000660.KS": [(1,22,16),(4,23,16),(7,23,16),(10,22,16)],
    "373220.KS": [(1,28,16),(4,28,16),(7,28,16),(10,28,16)],
    "005380.KS": [(1,27,15),(4,24,15),(7,24,15),(10,23,15)],
}

# Samsung preliminary (잠정실적) — always added, separate event from full results
SAMSUNG_PRELIM = [(1,7),(4,8),(7,8),(10,8)]   # ~5th-8th business day, 08:00 KST

def fetch_korean_earnings():
    out      = {}
    yf_count = 0

    # ── Yahoo Finance live dates ──────────────────────────────────────────────
    for ticker, (company, impact) in KOSPI_COMPANIES.items():
        fetched = False
        for base in ["https://query2.finance.yahoo.com",
                     "https://query1.finance.yahoo.com"]:
            url = (f"{base}/v10/finance/quoteSummary/{ticker}"
                   f"?modules=calendarEvents")
            try:
                r      = requests.get(url, timeout=10, headers=YF_HEADERS)
                r.raise_for_status()
                result = (r.json().get("quoteSummary", {})
                                  .get("result") or [])
                if not result: continue
                dates  = (result[0].get("calendarEvents", {})
                                   .get("earnings", {})
                                   .get("earningsDate", []))
                for ed in dates:
                    ts = ed.get("raw", 0)
                    if not ts: continue
                    dt_utc = datetime.fromtimestamp(ts, tz=UTC)
                    if dt_utc.year != YEAR: continue
                    # One entry per quarter (keyed by ticker + YYYYMM)
                    key = f"EARN|{ticker}|{dt_utc.strftime('%Y%m')}|yf"
                    if key not in out:
                        out[key] = make_event(
                            f"🇰🇷 {company} Earnings", impact,
                            dt_utc, source="Yahoo Finance", country="KR")
                        yf_count += 1
                fetched = True; break
            except Exception:
                continue

    # ── Hardcoded fallbacks (used when Yahoo Finance hasn't announced yet) ────
    for ticker, quarters in EARNINGS_FALLBACK.items():
        company, impact = KOSPI_COMPANIES[ticker]
        for month, day, hour_kst in quarters:
            # Only add fallback if Yahoo Finance didn't already provide this quarter
            qkey = f"EARN|{ticker}|{YEAR}{month:02d}"
            if any(k.startswith(qkey) for k in out):
                continue
            try:
                dt_utc = to_utc_kst(datetime(YEAR, month, day, hour_kst, 0))
                key    = f"EARN|{ticker}|{YEAR}{month:02d}|fallback"
                out[key] = make_event(
                    f"🇰🇷 {company} Earnings", impact,
                    dt_utc, source="Calculated (approx)", country="KR")
            except ValueError:
                pass

    # ── Samsung preliminary results (잠정실적) — always added ─────────────────
    # These are a separate, earlier event from the full results above.
    # Released ~08:00 KST, before market open (09:00 KST).
    for month, day in SAMSUNG_PRELIM:
        try:
            dt_utc = to_utc_kst(datetime(YEAR, month, day, 8, 0))
            key    = f"EARN|005930.KS|{YEAR}{month:02d}|prelim"
            out[key] = make_event(
                "🇰🇷 Samsung Prelim Results (잠정실적)", "High",
                dt_utc, source="Calculated (approx)", country="KR")
        except ValueError:
            pass

    total = len(out)
    calc  = total - yf_count
    print(f"  ✅ Korean Earnings: {total} events "
          f"({yf_count} live from Yahoo Finance, {calc} approximate fallback)")
    return out


# ── ICS builder ───────────────────────────────────────────────────────────────
# All event times are written in Asia/Singapore (SGT = UTC+8, no DST).
# This means times display correctly in SGT on any device / calendar client,
# regardless of the device's own timezone setting.

VTIMEZONE_SGT = "\r\n".join([
    "BEGIN:VTIMEZONE",
    "TZID:Asia/Singapore",
    "BEGIN:STANDARD",
    "DTSTART:19700101T000000",
    "TZOFFSETFROM:+0800",
    "TZOFFSETTO:+0800",
    "TZNAME:SGT",
    "END:STANDARD",
    "END:VTIMEZONE",
])

def fold(line, limit=75):
    if len(line) <= limit: return line
    parts = []
    while len(line) > limit:
        parts.append(line[:limit]); line = " " + line[limit:]
    parts.append(line)
    return "\r\n".join(parts)

def build_ics(events):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//USD+KRW Economic Calendar//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:📊 USD & KRW Economic Calendar",
        "X-WR-CALDESC:USA macro + Korea macro + KOSPI200 earnings — times in SGT",
        "X-WR-TIMEZONE:Asia/Singapore",          # Apple Calendar hint
        "REFRESH-INTERVAL;VALUE=DURATION:P1D",
        "X-PUBLISHED-TTL:P1D",
        VTIMEZONE_SGT,                            # embedded timezone definition
    ]
    for e in sorted(events.values(), key=lambda x: x["dt_utc"]):
        emoji   = IMPACT_EMOJI.get(e["impact"], "")
        dt_utc  = datetime.fromisoformat(e["dt_utc"])
        dt_sgt  = dt_utc.astimezone(SGT)          # convert to SGT
        end_sgt = dt_sgt + timedelta(hours=1)
        desc    = " | ".join(filter(None, [
            f"Forecast: {e['forecast']}" if e.get("forecast") else "",
            f"Previous: {e['previous']}" if e.get("previous") else "",
            f"Source: {e.get('source', '')}",
        ])) or "No forecast yet"

        lines += ["BEGIN:VEVENT",
                  f"UID:{e['uid']}@usd-krw-econ-cal",
                  f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            # All-day events: just a date, no timezone needed
            lines += [f"DTSTART;VALUE=DATE:{dt_sgt.strftime('%Y%m%d')}",
                      f"DTEND;VALUE=DATE:{(dt_sgt + timedelta(days=1)).strftime('%Y%m%d')}"]
        else:
            # Timed events: local SGT time with explicit TZID
            lines += [f"DTSTART;TZID=Asia/Singapore:{dt_sgt.strftime('%Y%m%dT%H%M%S')}",
                      f"DTEND;TZID=Asia/Singapore:{end_sgt.strftime('%Y%m%dT%H%M%S')}"]
        lines += [fold(f"SUMMARY:{emoji} {e['title']}"),
                  fold(f"DESCRIPTION:{desc}"),
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── cache helpers ─────────────────────────────────────────────────────────────

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
    # Clear all regenerated-fresh prefixes; keep only BLS carry-over
    cached = {k: v for k, v in cached.items()
              if not any(k.startswith(p) for p in ALWAYS_REFRESH)}
    print(f"📂 Cache: {len(cached)} carry-over events\n📡 Fetching all sources...")

    fresh = {}
    fresh.update(fetch_forexfactory())
    fresh.update(fetch_fomc())
    fresh.update(fetch_bls())
    fresh.update(fetch_bea())
    fresh.update(fetch_ism())
    fresh.update(fetch_conference_board())
    fresh.update(fetch_uom())
    fresh.update(fetch_bok())
    fresh.update(fetch_korean_earnings())

    merged = prune_old({**cached, **fresh})
    merged = deduplicate(merged)

    print(f"\n✅ Total: {len(merged)} events")
    json.dump(merged, open(CACHE_FILE, "w"), indent=2)
    with open(ICS_FILE, "w", encoding="utf-8") as f:
        f.write(build_ics(merged))
    print(f"📅 Written → {ICS_FILE}")

    now      = datetime.now(UTC).isoformat()
    upcoming = sorted([e for e in merged.values() if e["dt_utc"] >= now],
                      key=lambda x: x["dt_utc"])[:70]
    print("\n── Upcoming (SGT) ──")
    for e in upcoming:
        sgt  = datetime.fromisoformat(e["dt_utc"]).astimezone(SGT)
        flag = "🇰🇷" if e.get("country") == "KR" else "🇺🇸"
        print(f"  {IMPACT_EMOJI.get(e['impact'],'')} {flag} "
              f"{sgt.strftime('%d %b %H:%M')} SGT  "
              f"{e['title']:<48} [{e.get('source','')}]")
