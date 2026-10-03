"""Private-hospital doctor directories.

Deterministic HTML parsers for the hospitals we know (free, fast, repeatable). Each returns plain dicts:
    {name, specialty, hospital_key, hospital, profile_url, qualifications, source_url}
Adding a hospital = adding one parser function and one entry in config.HOSPITAL_SOURCES.
"""
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import config
from ..http import fetch


def _text(el):
    return el.get_text(" ", strip=True) if el else ""


def parse_hkah(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select(".doctor-item"):
        name = _text(item.select_one(".doctor-item-name"))
        if not name:
            continue
        profile = item.select_one("a.doctor-item-btn[href]")
        out.append({
            "name": name,
            "specialty": _text(item.select_one(".doctor-item-specialist-txt")),
            "title": _text(item.select_one(".doctor-item-department")),
            "qualifications": _text(item.select_one(".doctor-item-content-txt-b")),
            "profile_url": urljoin(base_url, profile["href"].split("#")[0]) if profile else None,
        })
    return out


def parse_matilda(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select(".jplist-item"):
        name = _text(item.select_one("h3.name"))
        if not name:
            continue
        spec = _text(item.select_one(".padding-16 .listing-autocomplete-hide-container"))
        link = item.select_one("a[href]")
        quals = item.select_one(".bg-grey .p-small")
        out.append({
            "name": " ".join(name.split()),
            "specialty": spec.replace("Specialist in ", "").strip(),
            "title": "",
            "qualifications": _text(quals).replace("Qualification", "").strip(),
            "profile_url": link["href"].split("?")[0] if link else None,
        })
    return out


PARSERS = {"hkah": parse_hkah, "matilda": parse_matilda}


def scrape(source: dict) -> tuple[list[dict], dict]:
    """Returns (doctors, fetch_info). Raises http.SourceUnavailable if the site is down and nothing is cached."""
    page = fetch(source["url"])
    doctors = PARSERS[source["parser"]](page.html, source["url"])
    hospital = config.PRIVATE_HOSPITALS[source["key"]]
    for d in doctors:
        d.update(hospital_key=source["key"], hospital=hospital, source_url=source["url"])
    return doctors, {"from_cache": page.from_cache, "stale": page.stale, "fetched_at": page.fetched_at}
