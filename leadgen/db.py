"""SQLite storage. One file, no server, easy to inspect in the demo."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request TEXT NOT NULL,
    criteria_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',      -- running | completed | failed
    started_at TEXT NOT NULL,
    finished_at TEXT,
    web_searches_used INTEGER NOT NULL DEFAULT 0
);

-- Every doctor record found in any source, before merging. Kept for audit.
CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,            -- e.g. hkah, matilda, fixture
    source_url TEXT,
    name_raw TEXT NOT NULL,
    specialty_raw TEXT,
    hospital TEXT,
    qualifications TEXT,
    email_raw TEXT,
    fetched_at TEXT NOT NULL,
    lead_id INTEGER                   -- which lead this record was merged into
);

-- One row per real-world doctor after deduplication.
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    mchk_reg_no TEXT,
    mchk_name TEXT,
    mchk_address TEXT,
    specialty TEXT,
    hospitals TEXT,                   -- JSON list
    location TEXT,
    profile_urls TEXT,                -- JSON list
    email TEXT,
    email_source_url TEXT,
    email_type TEXT,                  -- personal | clinic_generic | unknown
    email_status TEXT,                -- not_found | invalid | excluded_domain | unverified | risky | verified
    stage TEXT NOT NULL DEFAULT 'discovered',   -- discovered > registry_checked > enriched > verified > done
    lead_status TEXT NOT NULL DEFAULT 'PENDING', -- QUALIFIED | NEEDS_REVIEW | REJECTED | DUPLICATE | PENDING_ENRICHMENT | EXISTING_IN_CRM
    reason_codes TEXT NOT NULL DEFAULT '[]',     -- JSON list of machine-readable reasons
    notes TEXT NOT NULL DEFAULT '[]',            -- JSON list of human-readable verification notes
    duplicate_of INTEGER,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_run_reg ON leads(run_id, mchk_reg_no) WHERE mchk_reg_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    subject TEXT,
    body TEXT,
    status TEXT NOT NULL,             -- skipped | drafted | flagged | approved | rejected | sent | failed
    check_results TEXT,               -- JSON: availability / verification / previous contact / fact check
    reviewer TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    sent_at TEXT,
    message_id TEXT
);

-- Append-only event log: what happened, to which record, and why.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    lead_id INTEGER,
    level TEXT NOT NULL,              -- info | warn | error
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    at TEXT NOT NULL
);

-- Mock of the company's existing CRM and outreach history (loaded from data/*.csv).
CREATE TABLE IF NOT EXISTS crm_doctors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    mchk_reg_no TEXT,
    email TEXT,
    hospital TEXT,
    added_on TEXT
);
CREATE TABLE IF NOT EXISTS outreach_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    mchk_reg_no TEXT,
    contacted_on TEXT NOT NULL,
    campaign TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Commit on success, roll back on any error, so a crash never leaves a half-written stage."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def log(conn, code: str, message: str, *, run_id=None, lead_id=None, level="info"):
    conn.execute(
        "INSERT INTO events(run_id, lead_id, level, code, message, at) VALUES (?,?,?,?,?,?)",
        (run_id, lead_id, level, code, message, now()),
    )
    conn.commit()
    prefix = {"info": "  ", "warn": "! ", "error": "X "}[level]
    print(f"{prefix}[{code}] {message}")


def jload(value, default=None):
    if value is None:
        return default if default is not None else []
    return json.loads(value)


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False)
