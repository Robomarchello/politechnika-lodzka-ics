#!/usr/bin/env python3
"""Download a Celcat timetable (events.json) and convert it to an .ics file.

Default source: https://lodz.celcat.cloud/cal/events?44=1001   (1CSC - group 2)

Usage:
    python generate_ics.py                      # download + write calendar.ics
    python generate_ics.py --group 44           # other student-set id
    python generate_ics.py --input events.json  # use a local file (testing)

Only the Python standard library is needed (Python 3.9+).
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

GROUP_ID = "44"  # 1CSC - group 2
URL_TEMPLATE = "https://lodz.celcat.cloud/cal/events?{gid}=1001"
TZ = ZoneInfo("Europe/Warsaw")

# Celcat "type" ids used in the "names" lookup table
T_MODULE, T_STUDENT_SET, T_STAFF, T_FACILITY, T_CATEGORY = "1000", "1001", "1002", "1003", "1105"


# ----------------------------------------------------------------- download
def fetch_json(url, retries=3):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (celcat-ics-subscription)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"download attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise SystemExit(f"Could not download {url}: {last}")


# ------------------------------------------------------------------ helpers
def name_of(data, type_id, res_id):
    entry = data.get("names", {}).get(str(type_id), {}).get(str(res_id)) or {}
    return (entry.get("name") or entry.get("uniqueName") or "").strip()


def clean_module(name):
    # "Physics (26/27 W)" -> "Physics"
    return re.sub(r"\s*\(\d{2}/\d{2}\s*[WS]\)\s*$", "", name).strip()


def week0_monday(data, gid):
    """Monday of week index 0 (the first entry of every event's `weeks` array)."""
    ranges = data.get("viewableResourceDates", {}).get(str(gid))
    if not ranges:
        raise SystemExit("No viewableResourceDates in the JSON; pass --week0 YYYY-MM-DD")
    start = datetime.fromtimestamp(ranges[0]["startDate"] / 1000, tz=timezone.utc)
    start = (start + timedelta(hours=12)).date()  # tolerate midnight-in-other-timezone
    return start - timedelta(days=start.weekday())


def esc(text):
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line):
    """RFC 5545 line folding (75 octets, never splitting a UTF-8 character)."""
    raw = line.encode("utf-8")
    parts, limit = [], 75
    while len(raw) > limit:
        cut = limit
        while (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut])
        raw = raw[cut:]
        limit = 74
    parts.append(raw)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def utc_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------- convert
def build_events(data, gid, week0):
    out = []
    for ev in data.get("events", []):
        if ev.get("suspended"):
            continue
        if gid not in {s["id"] for s in ev.get("studentSets", [])}:
            continue

        module = next((clean_module(name_of(data, T_MODULE, m["id"])) for m in ev.get("modules", [])), "")
        category = name_of(data, T_CATEGORY, ev["eventCategoryId"]) if ev.get("eventCategoryId") else ""
        label = (ev.get("eventName") or "").strip()
        notes = (ev.get("notes") or "").strip()
        staff = [name_of(data, T_STAFF, s["id"]) for s in ev.get("staff", [])]
        rooms = [name_of(data, T_FACILITY, f["id"]) for f in ev.get("facilities", [])]

        # Summary: "Physics – Lecture"; fall back to the event name (e.g. "Academic Year Inauguration")
        if module:
            summary = f"{module} – {category}" if category else module
        else:
            summary = label or notes or "Class"

        # eventName is often only a note about weeks ("weeks 2-11") - not worth showing
        extras = []
        for txt in (label, notes):
            if txt and not re.match(r"^weeks?\s*\d", txt, re.I) and txt not in extras and txt != summary:
                extras.append(txt)

        start_ms = int(ev["startTime"])
        duration = int(ev["duration"])
        changed = ev.get("dateChanged") or 0
        stamp = datetime.fromtimestamp(changed / 1000, tz=timezone.utc)

        for week, active in enumerate(ev.get("weeks", [])):
            if not active:
                continue
            day = week0 + timedelta(weeks=week, days=int(ev["dayOfWeek"]) - 1)
            naive_start = datetime(day.year, day.month, day.day) + timedelta(milliseconds=start_ms)
            naive_end = naive_start + timedelta(minutes=duration)
            start = naive_start.replace(tzinfo=TZ)
            end = naive_end.replace(tzinfo=TZ)

            desc = []
            if category:
                desc.append(f"Type: {category}")
            if staff:
                desc.append("Lecturer: " + "; ".join(staff))
            desc.extend(extras)
            week_notes = ev.get("weekNotes") or []
            if week < len(week_notes) and week_notes[week]:
                desc.append(str(week_notes[week]))
            desc.append(f"Teaching week {week}")

            out.append(
                {
                    "uid": f"celcat-{ev['eventId']}-w{week}@celcat-ics-subscription",
                    "start": start,
                    "end": end,
                    "stamp": stamp,
                    "summary": summary,
                    "location": ", ".join(r for r in rooms if r),
                    "description": "\n".join(desc),
                }
            )
    out.sort(key=lambda e: (e["start"], e["uid"]))
    return out


def render_ics(events, cal_name):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//celcat-ics-subscription//Celcat Lodz//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(cal_name)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for e in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            # DTSTAMP comes from Celcat's "dateChanged", so the file is byte-identical
            # between runs unless the timetable really changed (no pointless commits).
            f"DTSTAMP:{utc_stamp(e['stamp'])}",
            f"LAST-MODIFIED:{utc_stamp(e['stamp'])}",
            f"DTSTART:{utc_stamp(e['start'])}",
            f"DTEND:{utc_stamp(e['end'])}",
            f"SUMMARY:{esc(e['summary'])}",
        ]
        if e["location"]:
            lines.append(f"LOCATION:{esc(e['location'])}")
        if e["description"]:
            lines.append(f"DESCRIPTION:{esc(e['description'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ----------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default=GROUP_ID, help="Celcat student-set id (default: 44 = 1CSC - group 2)")
    ap.add_argument("--output", default="calendar.ics")
    ap.add_argument("--input", help="read this local JSON file instead of downloading")
    ap.add_argument("--week0", help="override: Monday (YYYY-MM-DD) of week index 0")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = fetch_json(URL_TEMPLATE.format(gid=args.group))

    if args.week0:
        w0 = datetime.strptime(args.week0, "%Y-%m-%d").date()
    else:
        w0 = week0_monday(data, args.group)

    events = build_events(data, args.group, w0)
    if not events:
        raise SystemExit("No events found for this group - refusing to overwrite the calendar.")

    group_name = name_of(data, T_STUDENT_SET, args.group) or f"group {args.group}"
    ics = render_ics(events, f"{group_name} – IFE Łódź")
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(ics)
    print(f"Wrote {len(events)} events for '{group_name}' to {args.output} (week 0 = {w0})")


if __name__ == "__main__":
    main()
