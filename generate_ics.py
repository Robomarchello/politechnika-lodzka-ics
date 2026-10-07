import argparse
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- download
def fetch_json(url, retries=3):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (celcat-ics-subscription)",
            "Accept": "application/json, text/plain, */*",
        },
    )
    # Start with default verification context
    ctx = ssl.create_default_context()
    
    last = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            # Fall back to unverified context if the server has an incomplete cert chain
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                ctx = ssl._create_unverified_context()
            last = exc
            print(f"download attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise SystemExit(f"Could not download {url}: {last}")