"""Outreach workflow for qualified doctors.

Qualified Doctor -> Email Available? -> Email Verified? -> Previously Contacted? -> Generate Personalised Email
-> Human Approval -> Send Email -> Log Outreach

Nothing is sent without a person approving it in the web app (app.py). Default sending mode is
"simulate": the approved email is written to outbox/ as a .eml file instead of leaving the machine.

    python -m leadgen.outreach prepare [--run RUN_ID]   # checks + AI drafts for every qualified lead
"""
import argparse
import re
import smtplib
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from . import config, db, llm

FORBIDDEN = [
    r"as (we )?discussed", r"following up on our", r"(we|i) met", r"your (recent )?(paper|publication|research|study|award)",
    r"congratulations", r"as promised", r"per our (call|conversation)", r"guarantee",
]


def latest_run(conn) -> int:
    row = conn.execute("SELECT id FROM runs WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("No completed run yet. Run: python -m leadgen.pipeline")
    return row["id"]


def previous_contact(conn, lead) -> str | None:
    cutoff = (date.today() - timedelta(days=config.RECONTACT_COOLDOWN_DAYS)).isoformat()
    row = conn.execute(
        "SELECT contacted_on, campaign FROM outreach_history WHERE (email=? OR (mchk_reg_no IS NOT NULL AND mchk_reg_no=?)) "
        "ORDER BY contacted_on DESC LIMIT 1", ((lead["email"] or "").lower(), lead["mchk_reg_no"])).fetchone()
    if row and row["contacted_on"] >= cutoff:
        return f"contacted on {row['contacted_on']} ({row['campaign']}); cooldown is {config.RECONTACT_COOLDOWN_DAYS} days"
    row = conn.execute(
        "SELECT o.sent_at FROM outreach o JOIN leads l ON l.id=o.lead_id WHERE o.status='sent' AND "
        "(lower(o.email)=? OR (l.mchk_reg_no IS NOT NULL AND l.mchk_reg_no=?)) ORDER BY o.sent_at DESC LIMIT 1",
        ((lead["email"] or "").lower(), lead["mchk_reg_no"])).fetchone()
    if row and row["sent_at"][:10] >= cutoff:
        return f"already emailed by this system on {row['sent_at'][:10]}"
    return None


def fact_sheet(lead) -> dict:
    surname = (lead["mchk_name"] or "").split(",")[0].strip().title() or lead["name"].split()[-1]
    return {
        "salutation_name": f"Dr {surname}",
        "doctor_full_name": lead["name"],
        "specialty": "orthopaedics and traumatology",
        "hospitals": db.jload(lead["hospitals"]),
        "sender_org": config.CAMPAIGN["sender_org"],
        "about": config.CAMPAIGN["about"],
        "ask": config.CAMPAIGN["ask"],
        "signature": config.CAMPAIGN["signature"],
    }


def fact_check(draft: dict, facts: dict) -> list[str]:
    """Deterministic checks that the AI did not add facts. Returns a list of problems (empty = pass)."""
    body, subject = draft.get("body", ""), draft.get("subject", "")
    text = subject + "\n" + body
    problems = []
    if facts["salutation_name"] not in body:
        problems.append(f"Salutation '{facts['salutation_name']}' missing - wrong or invented name?")
    if re.search(r"[\w.+-]+@[\w-]+\.\w+", text):
        problems.append("Contains an email address")
    if re.search(r"https?://|www\.", text):
        problems.append("Contains a link")
    if re.search(r"\+?\d[\d\s-]{6,}\d", text):
        problems.append("Contains a phone-like number")
    allowed_numbers = set(re.findall(r"\d+", " ".join(str(v) for v in facts.values())))
    for n in re.findall(r"\d+", text):
        if n not in allowed_numbers:
            problems.append(f"Number '{n}' is not in the fact sheet")
    own = " ".join(facts["hospitals"]).lower()
    for h in config.PRIVATE_HOSPITALS.values():
        # Distinctive part of the name: "Gleneagles Hospital", "Adventist Hospital", "St. Paul's Hospital"...
        short = h.split(" (")[0].split(" –")[0].replace("Hong Kong", "").replace("  ", " ").strip()
        short = short.replace(" & Hospital", "").replace("International ", "")
        if short.lower() in text.lower() and short.lower() not in own.replace("international ", "").replace(" & hospital", ""):
            problems.append(f"Mentions {short}, which is not this doctor's hospital")
    for pat in FORBIDDEN:
        if re.search(pat, text, re.I):
            problems.append(f"Claims something not in the facts (matched '{pat}')")
    words = len(body.split())
    if not 60 <= words <= 260:
        problems.append(f"Body length {words} words is outside 60-260")
    if facts["signature"] not in body:
        problems.append("Signature missing")
    return problems


def prepare(conn, run_id=None):
    run_id = run_id or latest_run(conn)
    leads = [dict(r) for r in conn.execute("SELECT * FROM leads WHERE run_id=? AND lead_status='QUALIFIED' ORDER BY name", (run_id,))]
    print(f"Preparing outreach for {len(leads)} qualified leads (run {run_id})")
    for lead in leads:
        if conn.execute("SELECT 1 FROM outreach WHERE lead_id=? AND status IN ('drafted','flagged','approved','sent')", (lead["id"],)).fetchone():
            continue
        checks = {"email_available": bool(lead["email"]), "email_verified": lead["email_status"] == "verified"}
        reason = None
        if not checks["email_available"]:
            reason = "no email"
        elif not checks["email_verified"]:
            reason = f"email status is {lead['email_status']}"
        else:
            prev = previous_contact(conn, lead)
            checks["previously_contacted"] = prev
            if prev:
                reason = f"previously contacted: {prev}"
        if reason:
            conn.execute("INSERT INTO outreach(lead_id, email, status, check_results, created_at) VALUES (?,?,?,?,?)",
                         (lead["id"], lead["email"] or "", "skipped", db.jdump({**checks, "skip_reason": reason}), db.now()))
            conn.commit()
            db.log(conn, "outreach_skipped", f"{lead['name']}: {reason}", run_id=run_id, lead_id=lead["id"], level="warn")
            continue
        facts = fact_sheet(lead)
        try:
            draft = llm.draft_email(facts)
        except llm.ApiLimitReached as e:
            db.log(conn, "api_limit", f"Drafting stopped: {e}. Re-run prepare later.", run_id=run_id, level="warn")
            break
        problems = fact_check(draft, facts)
        checks.update(fact_check=problems or "passed", drafted_by=draft.get("_drafted_by"), facts=facts)
        status = "flagged" if problems else "drafted"
        conn.execute("INSERT INTO outreach(lead_id, email, subject, body, status, check_results, created_at) VALUES (?,?,?,?,?,?,?)",
                     (lead["id"], lead["email"], draft["subject"], draft["body"], status, db.jdump(checks), db.now()))
        conn.commit()
        db.log(conn, f"outreach_{status}", f"{lead['name']} <{lead['email']}>" + (f" - fact check: {problems}" if problems else ""),
               run_id=run_id, lead_id=lead["id"], level="warn" if problems else "info")


def decide(conn, outreach_id: int, decision: str, reviewer: str, subject=None, body=None):
    """Human approval step. decision in {approve, reject}. Edits are re-fact-checked before approval."""
    o = dict(conn.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone())
    if o["status"] not in ("drafted", "flagged"):
        raise ValueError(f"Outreach {outreach_id} is {o['status']}, not awaiting approval")
    checks = db.jload(o["check_results"], {})
    if subject is not None or body is not None:
        o["subject"], o["body"] = subject or o["subject"], body or o["body"]
        problems = fact_check({"subject": o["subject"], "body": o["body"]}, checks["facts"])
        checks["fact_check_after_edit"] = problems or "passed"
        if decision == "approve" and problems:
            conn.execute("UPDATE outreach SET subject=?, body=?, status='flagged', check_results=? WHERE id=?",
                         (o["subject"], o["body"], db.jdump(checks), outreach_id))
            conn.commit()
            return "flagged", problems
    status = "approved" if decision == "approve" else "rejected"
    conn.execute("UPDATE outreach SET subject=?, body=?, status=?, reviewer=?, decided_at=?, check_results=? WHERE id=?",
                 (o["subject"], o["body"], status, reviewer, db.now(), db.jdump(checks), outreach_id))
    conn.commit()
    db.log(conn, f"outreach_{status}", f"#{outreach_id} {o['email']} by {reviewer}", lead_id=o["lead_id"])
    return status, []


def send(conn, outreach_id: int) -> str:
    o = dict(conn.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone())
    if o["status"] != "approved":
        raise ValueError("Only approved emails can be sent")
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (o["lead_id"],)).fetchone()
    prev = previous_contact(conn, dict(lead))
    if prev:   # re-check right before sending, in case someone else emailed them meanwhile
        conn.execute("UPDATE outreach SET status='skipped', check_results=? WHERE id=?",
                     (db.jdump({**db.jload(o["check_results"], {}), "skip_reason": "previously contacted: " + prev}), outreach_id))
        conn.commit()
        return "skipped"
    msg = EmailMessage()
    msg["From"] = f"{config.SENDER_NAME} <{config.SENDER_EMAIL}>"
    to = config.TEST_RECIPIENT_OVERRIDE if config.SEND_MODE == "smtp" and config.TEST_RECIPIENT_OVERRIDE else o["email"]
    msg["To"] = to
    msg["Subject"] = o["subject"]
    msg["X-Original-Recipient"] = o["email"]
    msg_id = f"<{uuid.uuid4().hex}@medicpaths-prototype>"
    msg["Message-ID"] = msg_id
    msg.set_content(o["body"])
    try:
        if config.SEND_MODE == "smtp":
            if not config.TEST_RECIPIENT_OVERRIDE:
                raise RuntimeError("SMTP mode requires TEST_RECIPIENT_OVERRIDE so a demo never emails a real doctor")
            with smtplib.SMTP(config.SMTP["host"], config.SMTP["port"], timeout=30) as s:
                s.starttls()
                s.login(config.SMTP["user"], config.SMTP["password"])
                s.send_message(msg)
        else:
            config.OUTBOX_DIR.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (config.OUTBOX_DIR / f"{stamp}_outreach{outreach_id}.eml").write_bytes(bytes(msg))
    except Exception as e:  # noqa: BLE001 - any send failure is logged and retryable
        conn.execute("UPDATE outreach SET status='failed', check_results=? WHERE id=?",
                     (db.jdump({**db.jload(o["check_results"], {}), "send_error": str(e)}), outreach_id))
        conn.commit()
        db.log(conn, "send_failed", f"#{outreach_id}: {e}", lead_id=o["lead_id"], level="error")
        return "failed"
    conn.execute("UPDATE outreach SET status='sent', sent_at=?, message_id=? WHERE id=?", (db.now(), msg_id, outreach_id))
    conn.execute("INSERT INTO outreach_history(email, mchk_reg_no, contacted_on, campaign) VALUES (?,?,?,?)",
                 (o["email"], lead["mchk_reg_no"], date.today().isoformat(), "Speaker invitation (prototype)"))
    conn.commit()
    db.log(conn, "outreach_sent", f"#{outreach_id} to {o['email']} ({config.SEND_MODE}{' -> ' + to if to != o['email'] else ''})", lead_id=o["lead_id"])
    return "sent"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["prepare"])
    p.add_argument("--run", type=int)
    a = p.parse_args(argv)
    conn = db.connect()
    prepare(conn, a.run)


if __name__ == "__main__":
    main()
