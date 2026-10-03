"""Reviewer web app: lead list, review queue, outreach approval and the event log.

    python app.py      ->  http://127.0.0.1:5000
"""
from flask import Flask, flash, redirect, render_template, request, url_for

from leadgen import config, db, outreach

app = Flask(__name__)
app.secret_key = "prototype-only"


def conn():
    return db.connect()


def run_or_latest(c):
    rid = request.args.get("run", type=int)
    if rid:
        return rid
    row = c.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


@app.template_filter("jl")
def jl(v):
    return db.jload(v)


@app.route("/")
def dashboard():
    c = conn()
    rid = run_or_latest(c)
    runs = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10")]
    counts = dict(c.execute("SELECT lead_status, COUNT(*) FROM leads WHERE run_id=? GROUP BY lead_status", (rid,)).fetchall()) if rid else {}
    raw = c.execute("SELECT COUNT(*) FROM raw_records WHERE run_id=?", (rid,)).fetchone()[0] if rid else 0
    by_source = c.execute("SELECT source, COUNT(*) n FROM raw_records WHERE run_id=? GROUP BY source", (rid,)).fetchall() if rid else []
    email_counts = dict(c.execute("SELECT COALESCE(email_status,'not researched'), COUNT(*) FROM leads WHERE run_id=? GROUP BY 1", (rid,)).fetchall()) if rid else {}
    out_counts = dict(c.execute("SELECT o.status, COUNT(*) FROM outreach o JOIN leads l ON l.id=o.lead_id WHERE l.run_id=? GROUP BY o.status", (rid,)).fetchall()) if rid else {}
    return render_template("dashboard.html", runs=runs, rid=rid, counts=counts, raw=raw, by_source=by_source,
                           email_counts=email_counts, out_counts=out_counts, offline=config.OFFLINE, model=config.CLAUDE_MODEL,
                           send_mode=config.SEND_MODE)


@app.route("/leads")
def leads():
    c = conn()
    rid = run_or_latest(c)
    status = request.args.get("status")
    q = "SELECT * FROM leads WHERE run_id=?"
    args = [rid]
    if status:
        q += " AND lead_status=?"
        args.append(status)
    q += " ORDER BY CASE lead_status WHEN 'QUALIFIED' THEN 0 WHEN 'NEEDS_REVIEW' THEN 1 WHEN 'PENDING_ENRICHMENT' THEN 2 WHEN 'EXISTING_IN_CRM' THEN 3 ELSE 4 END, name"
    rows = [dict(r) for r in c.execute(q, args)]
    for r in rows:
        r["raw"] = [dict(x) for x in c.execute("SELECT source, name_raw, specialty_raw, source_url FROM raw_records WHERE lead_id=?", (r["id"],))]
    return render_template("leads.html", rows=rows, rid=rid, status=status)


@app.route("/outreach")
def outreach_queue():
    c = conn()
    rid = run_or_latest(c)
    rows = [dict(r) for r in c.execute(
        "SELECT o.*, l.name, l.hospitals, l.email_status, l.email_source_url FROM outreach o JOIN leads l ON l.id=o.lead_id "
        "WHERE l.run_id=? ORDER BY CASE o.status WHEN 'flagged' THEN 0 WHEN 'drafted' THEN 1 WHEN 'approved' THEN 2 ELSE 3 END, l.name", (rid,))]
    return render_template("outreach.html", rows=rows, rid=rid, send_mode=config.SEND_MODE)


@app.post("/outreach/prepare")
def outreach_prepare():
    c = conn()
    outreach.prepare(c, run_or_latest(c))
    flash("Outreach checks and drafts prepared.")
    return redirect(url_for("outreach_queue"))


@app.post("/outreach/<int:oid>/decide")
def outreach_decide(oid):
    c = conn()
    decision = request.form["decision"]
    reviewer = request.form.get("reviewer") or "reviewer"
    status, problems = outreach.decide(c, oid, decision, reviewer, request.form.get("subject"), request.form.get("body"))
    flash(f"#{oid}: {status}" + (f" - still failing checks: {'; '.join(problems)}" if problems else ""))
    return redirect(url_for("outreach_queue") + f"#o{oid}")


@app.post("/outreach/<int:oid>/send")
def outreach_send(oid):
    c = conn()
    result = outreach.send(c, oid)
    flash(f"#{oid}: {result}")
    return redirect(url_for("outreach_queue") + f"#o{oid}")


@app.route("/log")
def log():
    c = conn()
    rid = run_or_latest(c)
    rows = [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? OR run_id IS NULL ORDER BY id DESC LIMIT 500", (rid,))]
    return render_template("log.html", rows=rows, rid=rid)


if __name__ == "__main__":
    app.run(debug=False, port=5000)
