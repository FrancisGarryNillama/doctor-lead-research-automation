"""HTTP fetching with retries, backoff and a disk cache.

If a source is down, we retry with backoff; if it is still down we fall back to the last cached copy
(and say so in the notes); if there is no cached copy the source is reported as unavailable and the
run carries on with the other sources.
"""
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import config

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MedicPathsLeadResearch/0.1 (prototype; contact partnerships)",
    "Accept-Language": "en-HK,en;q=0.9",
}


class SourceUnavailable(Exception):
    pass


@dataclass
class Page:
    url: str
    html: str
    fetched_at: str
    from_cache: bool
    stale: bool          # True when the live fetch failed and we used an older cached copy


def _cache_path(url: str):
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    return config.CACHE_DIR / "http" / f"{h}.json"


def fetch(url: str, *, retries: int = 3, backoff: float = 1.5, max_age_hours: float = 24, timeout: int = 25) -> Page:
    path = _cache_path(url)
    cached = None
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])).total_seconds() / 3600
        if age_h <= max_age_hours:
            return Page(url, cached["html"], cached["fetched_at"], from_cache=True, stale=False)

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            if r.status_code >= 400:
                # 4xx other than 429 will not get better with retries.
                last_err = f"HTTP {r.status_code}"
                break
            r.encoding = r.apparent_encoding or "utf-8"
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"url": url, "fetched_at": fetched_at, "html": r.text}), encoding="utf-8")
            return Page(url, r.text, fetched_at, from_cache=False, stale=False)
        except (requests.RequestException, requests.HTTPError) as e:
            last_err = str(e)
            if attempt < retries:
                time.sleep(backoff ** attempt)

    if cached:
        return Page(url, cached["html"], cached["fetched_at"], from_cache=True, stale=True)
    raise SourceUnavailable(f"{url}: {last_err}")
