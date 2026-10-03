"""Name normalisation and matching for Hong Kong doctor names.

The same doctor is written differently by each source:
    HKAH:      "Dr. Mok Wah Hong, Daniel"        (surname, given names, English name)
    Matilda:   "Dr. Athena Au Cheuk Yiu"         (English name, surname, given names)
    MCHK:      "MOK, WAH HONG DANIEL"            (SURNAME, given names + English name)
    Others:    "Dr Wah-Hong Mok", "Prof. Daniel W.H. Mok"
So we compare *sets of name tokens*, never raw strings.
"""
import re
import unicodedata

from rapidfuzz import fuzz

TITLES = {
    "dr", "doctor", "prof", "professor", "mr", "mrs", "ms", "miss", "sir", "dame",
    "hon", "honorary", "assoc", "associate", "clinical", "jp", "bbs", "mh", "sbs",
}


def tokens(name: str) -> list[str]:
    name = unicodedata.normalize("NFKD", name or "")
    name = re.sub(r"\(.*?\)", " ", name)                 # drop "(formerly registered as ...)" etc.
    name = re.sub(r"[^A-Za-z\s\-,.']", " ", name)        # drop Chinese characters and symbols
    name = name.replace("-", " ").replace(".", " ").replace(",", " ").replace("'", "")
    out = [t.lower() for t in name.split() if t]
    return [t for t in out if t not in TITLES]


def key(name: str) -> str:
    """Order-independent key: 'Dr. Mok Wah Hong, Daniel' and 'MOK, WAH HONG DANIEL' -> 'daniel hong mok wah'."""
    return " ".join(sorted(tokens(name)))


def display(name: str) -> str:
    """Clean display form without the title."""
    t = tokens(name)
    return " ".join(w.capitalize() for w in t)


def register_surname(mchk_name: str) -> str:
    """MCHK writes 'SURNAME, GIVEN NAMES'."""
    return mchk_name.split(",")[0].strip().lower() if "," in mchk_name else ""


def score(a: str, b: str) -> float:
    """0-100 similarity of two names, order-independent.

    token_set_ratio is 100 when one name's tokens are a subset of the other's (e.g. a hospital site
    omitting the English name). token_sort_ratio penalises the extra tokens, which we use to break ties.
    """
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return 0.0
    return 0.7 * fuzz.token_set_ratio(ka, kb) + 0.3 * fuzz.token_sort_ratio(ka, kb)


def surname_compatible(candidate: str, mchk_name: str) -> bool:
    s = register_surname(mchk_name)
    if not s:
        return True
    cand = set(tokens(candidate))
    return all(part in cand for part in s.split())
