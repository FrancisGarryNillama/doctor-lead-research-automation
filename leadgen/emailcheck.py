"""Email validation and verification, without sending anything.

Levels (each one must pass before the next means anything):
  1. syntax        - looks like a real address
  2. excluded      - not an HKU-related domain (domain or any subdomain)
  3. domain        - the domain has MX (or A) records, so it can receive mail
  4. on_source     - the address appears on the page we cite as its source (catches AI-invented addresses)
  5. associated    - that page also names the doctor, or the address itself contains the doctor's name
Result: email_status in {invalid, excluded_domain, risky, unverified, verified}.

Production would add an SMTP/RCPT check through a verification service (ZeroBounce, NeverBounce, Kickbox);
we do not probe mail servers from a prototype.
"""
import functools
import html as htmlmod
import re

import dns.exception
import dns.resolver

from . import config, names

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
FIND_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def normalise(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().strip(".,;:<>()[]\"'").lower()
    e = e.replace("mailto:", "")
    return e or None


def syntax_ok(email: str) -> bool:
    if not EMAIL_RE.match(email) or ".." in email:
        return False
    local, domain = email.rsplit("@", 1)
    return 0 < len(local) <= 64 and len(domain) <= 255 and not local.startswith(".") and not local.endswith(".")


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[1].lower()


def excluded_domain(email: str) -> str | None:
    d = domain_of(email)
    for ex in config.EXCLUDED_EMAIL_DOMAINS:
        if d == ex or d.endswith("." + ex):
            return ex
    return None


@functools.lru_cache(maxsize=512)
def domain_accepts_mail(domain: str) -> tuple[bool | None, str]:
    """(True/False, detail). None means DNS itself failed, so we could not tell."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return True, "MX: " + ", ".join(sorted(str(r.exchange).rstrip(".") for r in answers)[:2])
    except dns.resolver.NXDOMAIN:
        return False, "domain does not exist"
    except dns.resolver.NoAnswer:
        try:
            dns.resolver.resolve(domain, "A", lifetime=8)
            return True, "no MX, but has an A record (mail may still be delivered)"
        except dns.exception.DNSException:
            return False, "no MX or A record"
    except dns.exception.DNSException as e:
        return None, f"DNS lookup failed: {type(e).__name__}"


def mailbox_type(email: str, doctor_name: str) -> str:
    local = email.split("@")[0].lower()
    parts = set(re.split(r"[._+-]", local))
    name_tokens = set(names.tokens(doctor_name))
    if parts & name_tokens or any(t in local for t in name_tokens if len(t) >= 4):
        return "personal"
    if parts & config.GENERIC_MAILBOXES or local in config.GENERIC_MAILBOXES:
        return "clinic_generic"
    return "unknown"


def _decode_cfemail(hexstr: str) -> str:
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))


def emails_in_html(page_html: str) -> set[str]:
    """All addresses on a page, including Cloudflare-protected ones and 'name [at] domain [dot] com'."""
    text = htmlmod.unescape(page_html)
    found = {normalise(m) for m in FIND_RE.findall(text)}
    for hexstr in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', page_html):
        try:
            found.add(normalise(_decode_cfemail(hexstr)))
        except ValueError:
            pass
    for m in re.finditer(r"([A-Za-z0-9._%+-]+)\s*[\[\(]\s*at\s*[\]\)]\s*([A-Za-z0-9-]+(?:\s*[\[\(]\s*dot\s*[\]\)]\s*[A-Za-z0-9-]+)+)", text, re.I):
        domain = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", m.group(2), flags=re.I)
        found.add(normalise(f"{m.group(1)}@{domain}"))
    # Drop image names like logo@2x.png that the regex picks up.
    return {e for e in found if e and not re.search(r"\.(png|jpe?g|gif|svg|webp)$", e)}


def page_mentions_doctor(page_html: str, doctor_name: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", htmlmod.unescape(page_html)).lower()
    text_tokens = set(re.findall(r"[a-z]+", text))
    toks = names.tokens(doctor_name)
    # Every name token must appear on the page (order-free), so "Dr Chan" alone does not count.
    return len(toks) >= 2 and all(t in text_tokens for t in toks)


def verify(email: str | None, doctor_name: str, source_html: str | None) -> dict:
    """Run every check and return a dict the pipeline stores and shows to reviewers."""
    result = {"email": normalise(email), "checks": {}, "status": None, "type": None, "notes": []}
    e = result["email"]
    if not e:
        result["status"] = "not_found"
        return result
    c = result["checks"]
    c["syntax"] = syntax_ok(e)
    if not c["syntax"]:
        result["status"] = "invalid"
        result["notes"].append(f"'{e}' is not a valid email address")
        return result
    ex = excluded_domain(e)
    c["excluded_domain"] = ex
    if ex:
        result["status"] = "excluded_domain"
        result["notes"].append(f"Email domain is HKU-related ({ex}); excluded by the request")
        return result
    ok, detail = domain_accepts_mail(domain_of(e))
    c["domain_accepts_mail"] = ok
    c["domain_detail"] = detail
    result["type"] = mailbox_type(e, doctor_name)
    if source_html is not None:
        c["on_source"] = e in emails_in_html(source_html)
        c["source_names_doctor"] = page_mentions_doctor(source_html, doctor_name)
    else:
        c["on_source"] = None
        c["source_names_doctor"] = None
    c["associated"] = bool(result["type"] == "personal" or c["source_names_doctor"])

    if ok is False:
        result["status"] = "invalid"
        result["notes"].append(f"Domain cannot receive email ({detail})")
    elif c["on_source"] is False:
        result["status"] = "unverified"
        result["notes"].append("Address was NOT found on the cited source page (possible AI error) - needs review")
    elif c["on_source"] is None:
        result["status"] = "unverified"
        result["notes"].append("Source page could not be fetched to confirm the address")
    elif not c["associated"]:
        result["status"] = "risky"
        result["notes"].append("Address is on the page, but the page does not clearly name this doctor")
    elif ok is None:
        result["status"] = "risky"
        result["notes"].append(detail)
    else:
        result["status"] = "verified"
        kind = "doctor's own address" if result["type"] == "personal" else "clinic/practice address"
        result["notes"].append(f"Verified: found on source page that names the doctor; {kind}; {detail}")
    return result
