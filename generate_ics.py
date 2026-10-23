import argparse
import hashlib
import html
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_URL = "https://lodz.celcat.cloud/cal/events?44=1001"
DEFAULT_OUT = "calendar.ics"
CAL_NAME = "1CSC - group 2 – IFE Łódź"
TZ = ZoneInfo("Europe/Warsaw")
UID_DOMAIN = "celcat-ics-subscription"


# ----------------------------------------------------------------- download
def fetch_json(url, retries=3):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (celcat-ics-subscription)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    ctx = ssl.create_default_context()

    last = None
    for attempt in range(1, retries + 1):
        body = b""
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                body = resp.read()
                print(f"HTTP {resp.status}, {resp.headers.get('Content-Type')}, "
                      f"{len(body)} bytes, final URL: {resp.geturl()}", file=sys.stderr)
                return json.loads(body.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                ctx = ssl._create_unverified_context()
            last = exc
            print(f"download attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            if body:
                print(f"body starts with: {body[:300]!r}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise SystemExit(f"Could not download {url}: {last}")


# ------------------------------------------------------------------ parsing
def find_events(data):
    """The endpoint returns a list of events (or a dict wrapping one)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("events", "data", "results", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise SystemExit(f"Unexpected JSON shape: {type(data).__name__}; run with --dump to inspect it")


def parse_dt(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:  # Celcat sends local wall-clock time
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)


def clean_lines(raw):
    """Turn Celcat's HTML description into a list of plain-text lines."""
    if not raw:
        return []
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", str(raw), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = (re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines())
    return [ln for ln in lines if ln]


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def norm(text):
    return re.sub(r"\s+", " ", str(text).replace("–", "-").replace("—", "-")).strip().lower()


LOCATION_HINT = re.compile(r"\b(building|room|auditorium|centre|center|on-?line|ife-\d+)\b", re.I)


def convert(ev):
    start = parse_dt(ev["start"])
    end = parse_dt(ev.get("end") or ev["start"])

    category = str(ev.get("eventCategory") or "").strip()
    modules = as_list(ev.get("modules"))
    sites = as_list(ev.get("sites"))
    lines = clean_lines(ev.get("description"))

    title = modules[0] if modules else ""
    if not title:
        title = next((ln for ln in lines if " - " in ln or " – " in ln),
                     lines[0] if lines else "Class")
    summary = title
    if category and category.lower() not in title.lower():
        summary = f"{title} – {category}"

    location = ", ".join(sites)
    if not location:
        location = next((ln for ln in lines if LOCATION_HINT.search(ln)), "")

    skip = {norm(x) for x in (summary, title, category, location, *sites) if x}
    desc = []
    if category:
        desc.append(f"Type: {category}")
    desc += [ln for ln in lines if norm(ln) not in skip]

    week = None
    for ln in desc:
        m = re.search(r"teaching week\s+(\d+)", ln, re.I)
        if m:
            week = m.group(1)
            break

    ev_id = ev.get("id")
    if ev_id in (None, ""):
        ev_id = hashlib.md5(f"{summary}{start}".encode("utf-8")).hexdigest()[:8]
    uid = f"celcat-{ev_id}-w{week}" if week is not None else f"celcat-{ev_id}-{start:%Y%m%dT%H%M}"

    return {
        "uid": uid,
        "start": start,
        "end": end,
        "SUMMARY": summary,
        "LOCATION": location,
        "DESCRIPTION": "\n".join(desc),
    }


# ---------------------------------------------------------------- ICS output
def esc(text):
    return (
        str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def fold(line, limit=75):
    """Fold a content line at `limit` octets (RFC 5545), never splitting a character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n".join(out)


def fmt(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def load_existing(path):
    """Read the previous calendar so unchanged events keep their old timestamps."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold
    events, cur = {}, None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur and "UID" in cur:
                events[cur["UID"]] = cur
            cur = None
        elif cur is not None and ":" in line:
            key, val = line.split(":", 1)
            cur[key.split(";")[0]] = val
    return events


SIG_KEYS = ("DTSTART", "DTEND", "SUMMARY", "LOCATION", "DESCRIPTION")


def build_ics(events, previous):
    now = fmt(datetime.now(timezone.utc))
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//celcat-ics-subscription//Celcat Lodz//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(CAL_NAME)}",
        "X-WR-TIMEZONE:Europe/Warsaw",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for e in events:
        props = {
            "DTSTART": fmt(e["start"]),
            "DTEND": fmt(e["end"]),
            "SUMMARY": esc(e["SUMMARY"]),
            "LOCATION": esc(e["LOCATION"]) if e["LOCATION"] else "",
            "DESCRIPTION": esc(e["DESCRIPTION"]) if e["DESCRIPTION"] else "",
        }
        old = previous.get(f"{e['uid']}@{UID_DOMAIN}")
        unchanged = old is not None and all(props[k] == old.get(k, "") for k in SIG_KEYS)
        stamp = old.get("DTSTAMP", now) if unchanged else now
        modified = old.get("LAST-MODIFIED", stamp) if unchanged else now

        out.append("BEGIN:VEVENT")
        out.append(f"UID:{e['uid']}@{UID_DOMAIN}")
        out.append(f"DTSTAMP:{stamp}")
        out.append(f"LAST-MODIFIED:{modified}")
        out.append(f"DTSTART:{props['DTSTART']}")
        out.append(f"DTEND:{props['DTEND']}")
        out.append(f"SUMMARY:{props['SUMMARY']}")
        if props["LOCATION"]:
            out.append(f"LOCATION:{props['LOCATION']}")
        if props["DESCRIPTION"]:
            out.append(f"DESCRIPTION:{props['DESCRIPTION']}")
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in out) + "\r\n"


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Build calendar.ics from Celcat JSON")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--input", help="read JSON from a local file instead of downloading")
    ap.add_argument("--dump", action="store_true", help="print the first raw events and exit")
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = fetch_json(args.url)

    raw_events = find_events(data)
    print(f"fetched {len(raw_events)} raw events")

    if args.dump:
        print(json.dumps(raw_events[:3], indent=2, ensure_ascii=False))
        return

    events, seen = [], set()
    for ev in raw_events:
        try:
            item = convert(ev)
        except (KeyError, ValueError) as exc:
            print(f"skipping event {ev.get('id')!r}: {exc!r}", file=sys.stderr)
            continue
        if item["uid"] in seen:  # same series id twice in one week
            item["uid"] += f"-{item['start']:%Y%m%dT%H%M}"
        seen.add(item["uid"])
        events.append(item)

    if not events:
        # Fail loudly instead of overwriting a good calendar with an empty one.
        raise SystemExit("No usable events returned; leaving the existing file untouched.")

    events.sort(key=lambda e: (e["start"], e["uid"]))
    ics = build_ics(events, load_existing(args.out))
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        fh.write(ics)
    print(f"wrote {len(events)} events to {args.out}")


if __name__ == "__main__":
    main()
