"""Central configuration. Business rules live here so they can be changed without touching pipeline code."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("LEADS_DB") or ROOT / "output" / "leads.db")
CACHE_DIR = ROOT / "cache"
OUTPUT_DIR = ROOT / "output"
OUTBOX_DIR = ROOT / "outbox"
DATA_DIR = ROOT / "data"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5").strip()
MAX_WEB_SEARCHES_PER_RUN = int(os.getenv("MAX_WEB_SEARCHES_PER_RUN", "60"))
OFFLINE = not ANTHROPIC_API_KEY

SEND_MODE = os.getenv("SEND_MODE", "simulate").strip()
SMTP = {
    "host": os.getenv("SMTP_HOST", ""),
    "port": int(os.getenv("SMTP_PORT", "587") or 587),
    "user": os.getenv("SMTP_USER", ""),
    "password": os.getenv("SMTP_PASSWORD", ""),
}
SENDER_NAME = os.getenv("SENDER_NAME", "Medic Paths Partnerships")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "partnerships@example.com")
TEST_RECIPIENT_OVERRIDE = os.getenv("TEST_RECIPIENT_OVERRIDE", "").strip()

# Default request from the brief. The pipeline parses it with Claude (or a rule-based fallback).
DEFAULT_REQUEST = (
    "Find 20 private-hospital orthopedic doctors in Hong Kong. Exclude HKU-related email domains. "
    "We need the doctor's name, specialty, hospital/clinic, profile/source URL, and email address."
)

# The 13 private hospitals in Hong Kong (Department of Health list). Anything else is not "private hospital".
PRIVATE_HOSPITALS = {
    "hkah": "Hong Kong Adventist Hospital",
    "hkah_tw": "Hong Kong Adventist Hospital – Tsuen Wan",
    "matilda": "Matilda International Hospital",
    "hksh": "Hong Kong Sanatorium & Hospital",
    "hkbh": "Hong Kong Baptist Hospital",
    "stpaul": "St. Paul's Hospital",
    "sth": "St. Teresa's Hospital",
    "canossa": "Canossa Hospital (Caritas)",
    "union": "Union Hospital",
    "evangel": "Evangel Hospital",
    "pbh": "Precious Blood Hospital (Caritas)",
    "gleneagles": "Gleneagles Hospital Hong Kong",
    "cuhkmc": "CUHK Medical Centre",
}

# Hospital directories the prototype scrapes. Each entry names a parser in leadgen/sources/hospitals.py.
HOSPITAL_SOURCES = [
    {"key": "hkah", "parser": "hkah", "url": "https://www.hkah.org.hk/en/find-a-doctor"},
    {"key": "matilda", "parser": "matilda", "url": "https://www.matilda.org/en/find-a-doctor"},
]

# Medical Council of Hong Kong specialist register, filtered to orthopaedics. Authoritative for
# "is this a registered doctor" and "is the specialty right".
MCHK_ORTHO_URL = (
    "https://www.mchk.org.hk/english/list_register/list.php"
    "?advancedsearch=Y&type=S&qualification=Orthopaedic&page={page}&ipp=20"
)

# Specialty vocabulary. A lead's specialty must normalise to one of these.
SPECIALTY_SYNONYMS = {
    "orthopaedics": [
        "orthopaedic", "orthopedic", "orthopaedics", "orthopedics", "orthopaedics & traumatology",
        "orthopaedic surgery", "orthopedic surgery", "traumatology", "spine surgery", "spinal orthopedic",
        "sports medicine", "joint replacement", "hand surgery", "scoliosis",
    ],
}

# HKU-related email domains. Matching is on the domain and every subdomain (e.g. connect.hku.hk).
EXCLUDED_EMAIL_DOMAINS = [
    "hku.hk",            # University of Hong Kong and all faculty subdomains (hku.hk, connect.hku.hk, ...)
    "hku-szh.org",       # HKU-Shenzhen Hospital
    "hkusua.hku.hk",
    "hkumed.hk",
]

GENERIC_MAILBOXES = {
    "info", "enquiry", "enquiries", "inquiry", "contact", "admin", "office", "clinic", "reception",
    "appointment", "appointments", "booking", "hello", "mail", "general", "secretary",
}

# Free-mail providers: allowed, but the email must appear on a page that names the doctor.
FREE_MAIL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.com.hk", "hotmail.com", "outlook.com", "netvigator.com"}

# Name matching thresholds (0–100).
NAME_MATCH_ACCEPT = 90      # at or above: same person
NAME_MATCH_REVIEW = 75      # between REVIEW and ACCEPT: possible match, human review

# Outreach rules.
RECONTACT_COOLDOWN_DAYS = 180

# Campaign brief used for the AI-drafted email. Assumption documented in DOCUMENTATION.md.
CAMPAIGN = {
    "sender_org": "Medic Paths",
    "about": (
        "Medic Paths is a medic-led education provider that helps secondary-school students who want to study "
        "medicine (academic support, admissions and interview preparation)."
    ),
    "ask": (
        "invite the doctor to give a 30-minute online talk (or Q&A) to Medic Paths students about orthopaedics "
        "as a career, at a time that suits them; no obligation"
    ),
    "signature": "Medic Paths Partnerships Team",
}

# Prefix for exported files (the edge-case harness sets "edge_cases_").
EXPORT_PREFIX = ""
