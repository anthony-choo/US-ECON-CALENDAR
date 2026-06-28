"""
USD + KRW Economic Calendar — Full Year Auto-Sync
US Sources:  ForexFactory · FOMC · BLS · BEA · ISM · Conference Board · U of Michigan
KR Sources:  ForexFactory KRW · Bank of Korea rate decisions
"""

import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import uuid, json, os, re
import calendar as cal_mod

EASTERN      = ZoneInfo("America/New_York")
KST          = ZoneInfo("Asia/Seoul")
SGT          = ZoneInfo("Asia/Singapore")
UTC          = timezone.utc
YEAR         = datetime.now().year
IMPACT_EMOJI = {"High": "🔴", "Medium": "🟡"}
CACHE_FILE   = "docs/events_cache.json"
ICS_FILE     = "docs/calendar.ics"
HEADERS      = {"User-Agent": "Mozilla/5.0 (EconCalBot/2.0)"}

# Keys starting with these prefixes are always regenerated (not kept from cache)
ALWAYS_REFRESH = ("FOMC|", "BOK|", "ISM|", "CB|", "UOM|", "BEA|")


# ── date helpers ──────────────────────────────────────────────────────────────

def to_utc_et(dt):  return dt.replace(tzinfo=EASTERN).astimezone(UTC)
def to_utc_kst(dt): return dt.replace(tzinfo=KST).astimezone(UTC)

def nth_weekday(year, month, weekday, n):
    """nth occurrence of weekday (0=Mon … 6=Sun) in month. None if not found."""
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
    """Last occurrence of weekday in month."""
    for d in range(cal_mod.monthrange(year, month)[1], 0, -1):
        day = datetime(year, month, d)
        if day.weekday() == weekday:
            return day
    return None

def nth_business_day(year, month, n, holidays=frozenset()):
    """nth Mon-Fri that isn't a holiday."""
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
    h = set()
    h.add(datetime(year, 1, 1).date())                          # New Year's
    d = nth_weekday(year, 1, 0, 3);  h.add(d.date()) if d else None  # MLK
    d = nth_weekday(year, 2, 0, 3);  h.add(d.date()) if d else None  # Presidents
    d = last_weekday(year, 5, 0);    h.add(d.date()) if d else None  # Memorial
    h.add(datetime(year, 6, 19).date())                         # Juneteenth
    j4 = datetime(year, 7, 4)
    if j4.weekday() == 5: h.add(datetime(year, 7, 3).date())   # Jul 4 Sat → Fri observed
    elif j4.weekday() == 6: h.add(datetime(year, 7, 5).date()) # Jul 4 Sun → Mon observed
    else: h.add(j4.date())
    d = nth_weekday(year, 9,  0, 1); h.add(d.date()) if d else None  # Labor Day
    d = nth_weekday(year, 10, 0, 2); h.add(d.date()) if d else None  # Columbus
    h.add(datetime(year, 11, 11).date())                        # Veterans
    d = nth_weekday(year, 11, 3, 4); h.add(d.date()) if d else None  # Thanksgiving
    h.add(datetime(year, 12, 25).date())                        # Christmas
    return h

def make_event(title, impact, dt_utc, all_day=False,
               forecast="", previous="", source="", country="US"):
    return {"title": title, "impact": impact,
            "dt_utc": dt_utc.isoformat(), "all_day": all_day,
            "forecast": forecast, "previous": previous,
            "source": source, "country": country,
            "uid": str(uuid.uuid4())}

def parse_date(text):
    for fmt in ("%B %d, %Y", "%b. %d, %Y", "%b %d, %Y",
                "%m/%d/%Y", "%Y-%m-%d", "%B %d %Y"):
        try: return datetime.strptime(text.strip(), fmt)
        except ValueError: pass
    return None


# ── SOURCE 1: ForexFactory (USD + KRW) ───────────────────────────────────────

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
            country_code = (ev.findtext("country") or "").strip()
            if country_code not in ("USD", "KRW"):
                continue
            impact = (ev.findtext("impact") or "").strip()
            if impact not in ("High", "Medium"):
                continue

            title    = (ev.findtext("title")    or "").strip()
            date_s   = (ev.findtext("date")     or "").strip()
            time_s   = (ev.findtext("time")     or "").strip().lower()
            forecast = (ev.findtext("forecast") or "").strip()
            previous = (ev.findtext("previous") or "").strip()

            try: date = datetime.strptime(date_s, "%m-%d-%Y")
            except ValueError: continue

            if not time_s or time_s in ("tentative","all day","tbd"):
                dt_utc, all_day = date.replace(tzinfo=UTC), True
            else:
                t = None
                for fmt in ("%I:%M%p","%H:%M"):
                    try: t = datetime.strptime(time_s, fmt); break
                    except ValueError: pass
                if t is None:
                    dt_utc, all_day = date.replace(tzinfo=UTC), True
                else:
                    # KRW times on FF are already in KST
                    if country_code == "KRW":
                        dt_naive = date.replace(hour=t.hour, minute=t.minute)
                        dt_utc   = to_utc_kst(dt_naive)
                    else:
                        dt_utc = to_utc_et(date.replace(hour=t.hour, minute=t.minute))
                    all_day = False

            flag  = "🇰🇷 " if country_code == "KRW" else ""
            key   = f"FF|{country_code}|{date_s}|{time_s}|{title}"
            ctry  = "KR" if country_code == "KRW" else "US"
            out[key] = make_event(f"{flag}{title}", impact, dt_utc, all_day,
                                  forecast, previous, "ForexFactory", ctry)

    us  = sum(1 for k in out if "|USD|" in k)
    kr  = sum(1 for k in out if "|KRW|" in k)
    print(f"  ✅ ForexFactory: {us} USD + {kr} KRW events")
    return out


# ── SOURCE 2: FOMC (hardcoded 2026, scraped for other years) ─────────────────

FOMC_2026 = [("January",28),("March",18),("April",29),("June",10),
             ("July",29),("September",16),("October",28),("December",9)]

def fetch_fomc():
    out = {}
    if YEAR == 2026:
        for month, day in FOMC_2026:
            dt = to_utc_et(datetime.strptime(f"{month} {day} 2026", "%B %d %Y")
                           .replace(hour=14, minute=0))
            out[f"FOMC|{dt.date()}"] = make_event(
                "🏛 FOMC Rate Decision","High",dt,source="Federal Reserve")
        print(f"  ✅ FOMC: {len(out)} events")
        return out
    # Scrape for other years
    try:
        r   = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                           timeout=15, headers=HEADERS)
        soup = BeautifulSoup(r.text,"html.parser")
        text = soup.get_text(" ")
        idx  = text.find(str(YEAR))
        if idx == -1: raise ValueError("year not found")
        months = (r"January|February|March|April|May|June|"
                  r"July|August|September|October|November|December")
        pat = re.compile(rf"({months})\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})",re.I)
        seen = set()
        for m in pat.finditer(text[idx:idx+5000]):
            mn = m.group(1).capitalize()
            if mn in seen: continue
            seen.add(mn)
            day = int(m.group(3))
            dt_naive = datetime.strptime(f"{mn} {day} {YEAR}","%B %d %Y").replace(hour=14)
            dt = to_utc_et(dt_naive)
            if dt.year == YEAR:
                out[f"FOMC|{dt.date()}"] = make_event(
                    "🏛 FOMC Rate Decision","High",dt,source="Federal Reserve")
    except Exception as e:
        print(f"  ⚠️  FOMC: {e}")
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
    "Import and Export":      ("Import/Export Prices","Medium"),
    "Productivity":           ("Productivity",      "Medium"),
}

def fetch_bls():
    out = {}
    url = f"https://www.bls.gov/schedule/{YEAR}/home.htm"
    try:
        r = requests.get(url,timeout=15,headers=HEADERS); r.raise_for_status()
        soup = BeautifulSoup(r.text,"html.parser")
    except Exception as e:
        print(f"  ⚠️  BLS: {e}"); return out
    dp = re.compile(r"(\w+\.?\s+\d{1,2},?\s*\d{4})|(\d{1,2}/\d{1,2}/\d{4})|(\d{4}-\d{2}-\d{2})",re.I)
    for tag in soup.find_all(["a","li","td","p"]):
        text = tag.get_text(" ",strip=True)
        if not text or len(text)>300: continue
        title,impact = None,"Medium"
        for kw,(t,i) in BLS_MAP.items():
            if kw.lower() in text.lower(): title,impact=t,i; break
        if not title: continue
        ctx = tag.parent.get_text(" ",strip=True) if tag.parent else text
        dm  = dp.search(ctx)
        if not dm: continue
        dt_naive = parse_date(dm.group(0))
        if not dt_naive or dt_naive.year!=YEAR: continue
        dt_utc = to_utc_et(dt_naive.replace(hour=8,minute=30))
        key = f"BLS|{dt_utc.date()}|{title}"
        if key not in out:
            out[key] = make_event(title,impact,dt_utc,source="BLS")
    # Fallback text scan
    if not out:
        lines=[l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
        for i,line in enumerate(lines):
            title,impact=None,"Medium"
            for kw,(t,imp) in BLS_MAP.items():
                if kw.lower() in line.lower(): title,impact=t,imp; break
            if not title: continue
            ctx=" ".join(lines[max(0,i-3):i+4]); dm=dp.search(ctx)
            if not dm: continue
            dt_naive=parse_date(dm.group(0))
            if not dt_naive or dt_naive.year!=YEAR: continue
            dt_utc=to_utc_et(dt_naive.replace(hour=8,minute=30))
            key=f"BLS|{dt_utc.date()}|{title}"
            if key not in out: out[key]=make_event(title,impact,dt_utc,source="BLS")
    print(f"  ✅ BLS: {len(out)} events")
    return out


# ── SOURCE 4: BEA (hardcoded 2026) ───────────────────────────────────────────

BEA_2026 = [
    ("GDP (Advance)",        "High", 1,29), ("GDP (2nd Est.)",       "High", 2,26),
    ("GDP (3rd Est.)",       "High", 3,26), ("GDP (Advance)",        "High", 4,29),
    ("GDP (2nd Est.)",       "High", 5,28), ("GDP (3rd Est.)",       "High", 6,25),
    ("GDP (Advance)",        "High", 7,30), ("GDP (2nd Est.)",       "High", 8,27),
    ("GDP (3rd Est.)",       "High", 9,24), ("GDP (Advance)",        "High",10,29),
    ("GDP (2nd Est.)",       "High",11,24), ("GDP (3rd Est.)",       "High",12,22),
    ("PCE / Personal Income","High", 1,30), ("PCE / Personal Income","High", 2,27),
    ("PCE / Personal Income","High", 3,28), ("PCE / Personal Income","High", 4,30),
    ("PCE / Personal Income","High", 5,29), ("PCE / Personal Income","High", 6,26),
    ("PCE / Personal Income","High", 7,31), ("PCE / Personal Income","High", 8,28),
    ("PCE / Personal Income","High", 9,25), ("PCE / Personal Income","High",10,30),
    ("PCE / Personal Income","High",11,25), ("PCE / Personal Income","High",12,23),
]

def fetch_bea():
    out = {}
    # Try ICS feeds
    for url in ["https://www.bea.gov/news/schedule/icalendar",
                "https://www.bea.gov/icalendar/bea-release-calendar.ics"]:
        try:
            r = requests.get(url,timeout=15,headers=HEADERS); r.raise_for_status()
            if "BEGIN:VEVENT" not in r.text: continue
            kws = {"gross domestic product":("GDP","High"),
                   "personal income":("PCE / Personal Income","High"),
                   "international trade":("Trade Balance","Medium")}
            for block in r.text.split("BEGIN:VEVENT")[1:]:
                sm = re.search(r"SUMMARY[^:\r\n]*:([^\r\n]+)",block)
                if not sm: continue
                title,impact=None,"Medium"
                for kw,(t,i) in kws.items():
                    if kw in sm.group(1).lower(): title,impact=t,i; break
                if not title: continue
                dm = re.search(r"DTSTART[^:\r\n]*:(\d{8})",block)
                if not dm: continue
                try: dt_n=datetime.strptime(dm.group(1),"%Y%m%d")
                except ValueError: continue
                if dt_n.year!=YEAR: continue
                dt_utc=to_utc_et(dt_n.replace(hour=8,minute=30))
                key=f"BEA|{dt_utc.date()}|{title}"
                if key not in out: out[key]=make_event(title,impact,dt_utc,source="BEA")
            if out: print(f"  ✅ BEA: {len(out)} events (ICS)"); return out
        except Exception: pass
    # Hardcoded 2026 fallback
    if YEAR==2026:
        for title,impact,month,day in BEA_2026:
            try:
                dt_utc=to_utc_et(datetime(YEAR,month,day,8,30))
                key=f"BEA|{dt_utc.date()}|{title}|{month}"
                out[key]=make_event(title,impact,dt_utc,source="BEA")
            except ValueError: pass
    print(f"  ✅ BEA: {len(out)} events")
    return out


# ── SOURCE 5: ISM (calculated) ────────────────────────────────────────────────

def fetch_ism():
    """ISM Manufacturing = 1st business day. ISM Services = 3rd business day. 10 AM ET."""
    out  = {}
    hols = us_holidays(YEAR)
    for month in range(1,13):
        d1 = nth_business_day(YEAR,month,1,hols)
        if d1:
            dt = to_utc_et(d1.replace(hour=10,minute=0))
            out[f"ISM|MFG|{dt.date()}"] = make_event(
                "ISM Manufacturing PMI","Medium",dt,source="ISM")
        d3 = nth_business_day(YEAR,month,3,hols)
        if d3:
            dt = to_utc_et(d3.replace(hour=10,minute=0))
            out[f"ISM|SVC|{dt.date()}"] = make_event(
                "ISM Services PMI","Medium",dt,source="ISM")
    print(f"  ✅ ISM: {len(out)} events")
    return out


# ── SOURCE 6: Conference Board Consumer Confidence (calculated) ───────────────

def fetch_conference_board():
    """Last Tuesday of each month, 10 AM ET."""
    out = {}
    for month in range(1,13):
        d = last_weekday(YEAR,month,1)  # Tuesday=1
        if d:
            dt = to_utc_et(d.replace(hour=10,minute=0))
            out[f"CB|{dt.date()}"] = make_event(
                "Consumer Confidence (CB)","Medium",dt,source="Conference Board")
    print(f"  ✅ Conference Board: {len(out)} events")
    return out


# ── SOURCE 7: U of Michigan Consumer Sentiment (calculated) ──────────────────

def fetch_uom():
    """Preliminary = 2nd Friday, Final = 4th Friday of month, 10 AM ET."""
    out = {}
    for month in range(1,13):
        for n,label in [(2,"Prelim"),(4,"Final")]:
            d = nth_weekday(YEAR,month,4,n)  # Friday=4
            if d:
                dt = to_utc_et(d.replace(hour=10,minute=0))
                out[f"UOM|{label}|{dt.date()}"] = make_event(
                    f"Consumer Sentiment UoM ({label})","Medium",
                    dt,source="U of Michigan")
    print(f"  ✅ U of Michigan: {len(out)} events")
    return out


# ── SOURCE 8: Bank of Korea (calculated 3rd Thursday, 8 months) ──────────────
# BOK meets Jan Feb Apr May Jul Aug Oct Nov. Announces 10:00 AM KST.

BOK_MONTHS_2026 = [1,2,4,5,7,8,10,11]

def fetch_bok():
    out  = {}
    hols = {YEAR: BOK_MONTHS_2026}
    for month in BOK_MONTHS_2026:
        d = nth_weekday(YEAR,month,3,3)   # 3rd Thursday
        if d:
            dt = to_utc_kst(d.replace(hour=10,minute=0))
            out[f"BOK|{dt.date()}"] = make_event(
                "🇰🇷 BOK Interest Rate Decision","High",
                dt,source="Bank of Korea",country="KR")
    print(f"  ✅ BOK: {len(out)} events")
    return out


# ── ICS builder ───────────────────────────────────────────────────────────────

def fold(line,limit=75):
    if len(line)<=limit: return line
    parts=[]
    while len(line)>limit:
        parts.append(line[:limit]); line=" "+line[limit:]
    parts.append(line)
    return "\r\n".join(parts)

def build_ics(events):
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR","VERSION:2.0",
             "PRODID:-//USD+KRW Economic Calendar//EN",
             "CALSCALE:GREGORIAN","METHOD:PUBLISH",
             "X-WR-CALNAME:📊 USD & KRW Economic Calendar",
             "X-WR-CALDESC:USA + Korea medium/high impact events",
             "REFRESH-INTERVAL;VALUE=DURATION:P1D","X-PUBLISHED-TTL:P1D"]
    for e in sorted(events.values(),key=lambda x:x["dt_utc"]):
        emoji = IMPACT_EMOJI.get(e["impact"],"")
        dt    = datetime.fromisoformat(e["dt_utc"])
        end   = dt+timedelta(hours=1)
        desc  = " | ".join(filter(None,[
            f"Forecast: {e['forecast']}" if e.get("forecast") else "",
            f"Previous: {e['previous']}" if e.get("previous") else "",
            f"Source: {e.get('source','')}"
        ])) or "No forecast yet"
        lines+=["BEGIN:VEVENT",f"UID:{e['uid']}@usd-krw-econ-cal",f"DTSTAMP:{stamp}"]
        if e["all_day"]:
            lines+=[f"DTSTART;VALUE=DATE:{dt.strftime('%Y%m%d')}",
                    f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"]
        else:
            lines+=[f"DTSTART:{dt.strftime('%Y%m%dT%H%M%SZ')}",
                    f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}"]
        lines+=[fold(f"SUMMARY:{emoji} {e['title']}"),
                fold(f"DESCRIPTION:{desc}"),"END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ── cache ─────────────────────────────────────────────────────────────────────

def load_cache():
    try:
        with open(CACHE_FILE) as f: return json.load(f)
    except Exception: return {}

def prune_old(events):
    cutoff=(datetime.now(UTC)-timedelta(days=7)).isoformat()
    return {k:v for k,v in events.items() if v["dt_utc"]>=cutoff}


# ── main ──────────────────────────────────────────────────────────────────────

if __name__=="__main__":
    os.makedirs("docs",exist_ok=True)

    cached = load_cache()
    # Clear entries that are always recalculated fresh
    cached = {k:v for k,v in cached.items()
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

    merged = prune_old({**cached,**fresh})
    print(f"\n✅ Total: {len(merged)} events")

    json.dump(merged,open(CACHE_FILE,"w"),indent=2)
    with open(ICS_FILE,"w",encoding="utf-8") as f:
        f.write(build_ics(merged))
    print(f"📅 Written → {ICS_FILE}")

    now = datetime.now(UTC).isoformat()
    upcoming = sorted([e for e in merged.values() if e["dt_utc"]>=now],
                      key=lambda x:x["dt_utc"])[:60]
    print("\n── Upcoming (SGT) ──")
    for e in upcoming:
        sgt=datetime.fromisoformat(e["dt_utc"]).astimezone(SGT)
        print(f"  {IMPACT_EMOJI.get(e['impact'],'')} "
              f"{sgt.strftime('%d %b %H:%M')} SGT  "
              f"{e['title']:<40} [{e.get('source','')}]")
