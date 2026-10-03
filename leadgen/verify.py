"""Qualification rules. Pure functions over a lead dict, so they are easy to test and to explain.

A lead is QUALIFIED only when every rule passes. Anything uncertain becomes NEEDS_REVIEW with a reason,
never a silent accept. Hard failures become REJECTED.
"""
from . import config

# reason code -> (outcome, human explanation)
REASONS = {
    "NOT_IN_REGISTER":        ("REJECTED", "No matching doctor in the Medical Council specialist register (orthopaedics)"),
    "REGISTER_MATCH_UNSURE":  ("NEEDS_REVIEW", "Name only partly matches the register, or matches more than one doctor"),
    "WRONG_SPECIALTY":        ("REJECTED", "Specialty is not orthopaedics"),
    "SPECIALTY_CONFLICT":     ("NEEDS_REVIEW", "Sources disagree on specialty (register wins, but a person should look)"),
    "NOT_PRIVATE_HOSPITAL":   ("REJECTED", "Not listed by an eligible Hong Kong private hospital"),
    "OUTSIDE_LOCATION":       ("REJECTED", "Not practising in Hong Kong"),
    "EXISTING_IN_CRM":        ("EXISTING_IN_CRM", "Doctor is already in our database"),
    "NO_EMAIL":               ("NEEDS_REVIEW", "No public email found"),
    "INVALID_EMAIL":          ("NEEDS_REVIEW", "Email is invalid or its domain cannot receive mail"),
    "EXCLUDED_EMAIL_DOMAIN":  ("REJECTED", "Only email found is on an HKU-related domain"),
    "EMAIL_UNVERIFIED":       ("NEEDS_REVIEW", "Email could not be confirmed on its source page"),
    "EMAIL_RISKY":            ("NEEDS_REVIEW", "Email found but not clearly linked to this doctor"),
    "SHARED_EMAIL":           ("NEEDS_REVIEW", "Same email is used by another doctor in this list"),
    "EMAIL_IN_CRM":           ("EXISTING_IN_CRM", "This email already belongs to a doctor in our database"),
    "PENDING_ENRICHMENT":     ("PENDING_ENRICHMENT", "Contact research not done yet (API limit/budget); retried next run"),
    "SOURCE_STALE":           ("INFO", "Hospital page was down; used the last cached copy"),
    "MISSING_FIELDS":         ("NEEDS_REVIEW", "A required output field is empty"),
}

# Worst outcome wins.
SEVERITY = ["QUALIFIED", "INFO", "PENDING_ENRICHMENT", "NEEDS_REVIEW", "EXISTING_IN_CRM", "REJECTED"]


def specialty_matches(text: str, wanted: str = "orthopaedics") -> bool:
    t = (text or "").lower()
    return any(s in t for s in config.SPECIALTY_SYNONYMS.get(wanted, [wanted]))


def outcome(reason_codes: list[str]) -> str:
    worst = "QUALIFIED"
    for code in reason_codes:
        o = REASONS[code][0]
        if SEVERITY.index(o) > SEVERITY.index(worst):
            worst = o
    return "QUALIFIED" if worst == "INFO" else worst


def required_fields_present(lead: dict) -> list[str]:
    missing = []
    for field in ("name", "specialty", "hospitals", "profile_urls", "email", "location"):
        if not lead.get(field):
            missing.append(field)
    return missing
