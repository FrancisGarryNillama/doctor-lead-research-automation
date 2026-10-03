"""Medical Council of Hong Kong (MCHK) specialist register.

The register is the source of truth for two questions the brief asks:
  * Is this person actually a registered doctor?
  * Is their specialty really orthopaedics?
We download the orthopaedic part of the register once (about 30 pages), cache it, and match locally.
"""
import json
import re

from bs4 import BeautifulSoup

from .. import config, names
from ..http import SourceUnavailable, fetch

CACHE = config.CACHE_DIR / "mchk_ortho_register.json"
REG_NO = re.compile(r"^S\d{2}-\d{4}$")


def _parse_page(html: str) -> tuple[list[dict], bool]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
        if not cells or not REG_NO.match(cells[0]):
            continue
        # Columns: reg no | Chinese + English name | address | specialty | qualification | year (with spacer cells)
        cells = [c for c in cells]
        name_cell = cells[1]
        english = re.sub(r"^[^\x00-\x7F]+\s*", "", name_cell).strip()   # drop leading Chinese name
        chinese = name_cell[: len(name_cell) - len(english)].strip()
        rest = [c for c in cells[2:] if c]
        address = next((c for c in rest if re.search(r"(?i)hong kong|kowloon|new territories|room|floor|/f", c)), "")
        specialty = next((c for c in rest if "Orthopaedic" in c and "FHKAM" not in c), "")
        qual = next((c for c in rest if "FHKAM" in c or "Fellow" in c), "")
        year = next((c for c in rest if re.fullmatch(r"\d{4}", c)), "")
        rows.append({
            "reg_no": cells[0], "name": english, "chinese_name": chinese,
            "address": address, "specialty": specialty or "Orthopaedics & Traumatology",
            "qualification": qual, "year": year,
        })
    has_next = any("Next" in a.get_text() and a.get("href") for a in soup.find_all("a"))
    return rows, has_next


def load_register(refresh: bool = False, max_pages: int = 60) -> list[dict]:
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    all_rows, page = [], 1
    while page <= max_pages:
        html = fetch(config.MCHK_ORTHO_URL.format(page=page), max_age_hours=24 * 7).html
        rows, has_next = _parse_page(html)
        all_rows.extend(rows)
        if not rows or not has_next:
            break
        page += 1
    if not all_rows:
        raise SourceUnavailable("MCHK register returned no rows")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return all_rows


def match(candidate_name: str, register: list[dict]) -> dict:
    """Find the register entry for a doctor name.

    Returns {"status": "matched"|"ambiguous"|"possible"|"not_found", "entry": ..., "score": ..., "alternatives": [...]}
    """
    scored = []
    for e in register:
        if not names.surname_compatible(candidate_name, e["name"]):
            continue
        s = names.score(candidate_name, e["name"])
        if s >= config.NAME_MATCH_REVIEW:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {"status": "not_found", "entry": None, "score": 0, "alternatives": []}
    best_score, best = scored[0]
    close = [e for s, e in scored[1:] if best_score - s < 3]
    alts = [{"reg_no": e["reg_no"], "name": e["name"]} for _, e in scored[1:4]]
    if best_score >= config.NAME_MATCH_ACCEPT and not close:
        return {"status": "matched", "entry": best, "score": round(best_score, 1), "alternatives": alts}
    if close:
        return {"status": "ambiguous", "entry": best, "score": round(best_score, 1), "alternatives": alts}
    return {"status": "possible", "entry": best, "score": round(best_score, 1), "alternatives": alts}
