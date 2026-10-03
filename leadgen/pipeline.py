"""Lead research pipeline.

Request -> Search -> Find Doctors -> Enrich -> Verify -> Apply Requirements -> Deduplicate -> Quality Check -> Store

Stages run per lead and record progress in leads.stage, committed after every lead. If the process dies
halfway (network, crash, Ctrl-C), `python -m leadgen.pipeline --resume <run_id>` carries on from the last
completed lead instead of starting again or creating duplicates.

Usage:
    python -m leadgen.pipeline                      # run the default request from the brief
    python -m leadgen.pipeline --request "Find 10 ..."
    python -m leadgen.pipeline --resume 3
    python -m leadgen.pipeline --crash-after 5      # demo: simulate a crash after 5 enrichments
"""
import argparse
import csv
import sys
from collections import defaultdict

from . import config, db, emailcheck, export, llm, names, verify
from .http import SourceUnavailable, fetch
from .sources import hospitals, mchk


class SimulatedCrash(RuntimeError):
    pass


class Pipeline:
    def __init__(self, conn, *, research_fn=None, fetch_fn=None, register=None, sources=None, extra_records=None):
        self.conn = conn
        self.research = research_fn or llm.find_contact
        self.fetch = fetch_fn or fetch
        self._register = register
        self.sources = sources if sources is not None else config.HOSPITAL_SOURCES
        self.extra_records = extra_records or []   # injected records (edge-case fixtures)
        self.budget = llm.Budget(config.MAX_WEB_SEARCHES_PER_RUN)
        self.run_id = None
        self.criteria = None

    # ------------------------------------------------------------------ helpers
    def log(self, code, msg, lead_id=None, level="info"):
        db.log(self.conn, code, msg, run_id=self.run_id, lead_id=lead_id, level=level)

    @property
    def register(self):
        if self._register is None:
            self._register = mchk.load_register()
        return self._register

    def _lead(self, lead_id) -> dict:
        r = dict(self.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        for k in ("hospitals", "profile_urls", "reason_codes", "notes"):
            r[k] = db.jload(r[k])
        return r

    def _save(self, lead: dict, **changes):
        lead.update(changes)
        lead["updated_at"] = db.now()
        cols = [c for c in lead if c != "id"]
        vals = [db.jdump(lead[c]) if c in ("hospitals", "profile_urls", "reason_codes", "notes") else lead[c] for c in cols]
        self.conn.execute(f"UPDATE leads SET {', '.join(c + '=?' for c in cols)} WHERE id=?", (*vals, lead["id"]))
        self.conn.commit()

    @staticmethod
    def _add_reason(lead, code, note=None):
        if code not in lead["reason_codes"]:
            lead["reason_codes"].append(code)
        lead["notes"].append(note or verify.REASONS[code][1])

    # ------------------------------------------------------------------ stage 0: request
    def start(self, request: str):
        criteria = llm.parse_request(request)
        if criteria["specialty"] not in config.SPECIALTY_SYNONYMS:
            raise SystemExit(f"Unsupported specialty in request: {criteria['specialty']}")
        cur = self.conn.execute(
            "INSERT INTO runs(request, criteria_json, started_at) VALUES (?,?,?)",
            (request, db.jdump(criteria), db.now()),
        )
        self.conn.commit()
        self.run_id = cur.lastrowid
        self.criteria = criteria
        self.log("run_started", f"Run {self.run_id}: {criteria}")

    def resume(self, run_id: int):
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise SystemExit(f"No run {run_id}")
        self.run_id = run_id
        self.criteria = db.jload(row["criteria_json"], {})
        self.budget.max_searches += row["web_searches_used"]   # the cap is per run, not per process
        self.budget.used = row["web_searches_used"]
        self.conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        self.conn.commit()
        self.log("run_resumed", f"Resuming run {run_id}")

    # ------------------------------------------------------------------ stage 1: search + find doctors
    def discover(self):
        already = self.conn.execute("SELECT COUNT(*) FROM raw_records WHERE run_id=?", (self.run_id,)).fetchone()[0]
        if already:
            self.log("discover_skipped", f"{already} raw records already stored for this run")
            return
        wanted = self.criteria["specialty"]
        found = []
        for src in self.sources:
            try:
                doctors, info = hospitals.scrape(src)
            except SourceUnavailable as e:
                self.log("source_unavailable", f"{src['key']}: {e}. Continuing with the other sources.", level="warn")
                continue
            if info["stale"]:
                self.log("source_stale", f"{src['key']} is down; using cached copy from {info['fetched_at']}", level="warn")
            matching = [d for d in doctors if verify.specialty_matches(d["specialty"], wanted)]
            self.log("source_scraped", f"{src['key']}: {len(doctors)} doctors listed, {len(matching)} match '{wanted}'")
            for d in matching:
                d["stale"] = info["stale"]
            found.extend(matching)
        for d in self.extra_records:
            if not verify.specialty_matches(d["specialty"], wanted):
                self.log("wrong_specialty", f"{d['name']} ({d['specialty']}) at {d['hospital']} skipped: not {wanted}")
                continue
            d.setdefault("stale", False)
            found.append(d)
        if not found:
            raise SystemExit("No doctors found in any source")
        with db.tx(self.conn):
            for d in found:
                self.conn.execute(
                    "INSERT INTO raw_records(run_id, source, source_url, name_raw, specialty_raw, hospital, qualifications, email_raw, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.run_id, d["hospital_key"], d.get("profile_url") or d.get("source_url"), d["name"], d["specialty"],
                     d["hospital"], d.get("qualifications", ""), d.get("email"), db.now()),
                )
        self.raw_meta = {d["name"] + "|" + d["hospital_key"]: d for d in found}

    # ------------------------------------------------------------------ stage 2: identity check + dedupe
    def identify_and_dedupe(self):
        """Match every raw record to the MCHK register, then merge records that are the same doctor."""
        if self.conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=?", (self.run_id,)).fetchone()[0]:
            self.log("dedupe_skipped", "leads already built for this run")
            return
        raws = [dict(r) for r in self.conn.execute("SELECT * FROM raw_records WHERE run_id=?", (self.run_id,))]
        meta = getattr(self, "raw_meta", {})
        groups = defaultdict(list)    # merge key -> raw records
        matches = {}
        for r in raws:
            if r["hospital"] not in config.PRIVATE_HOSPITALS.values():
                matches[r["id"]] = {"status": "not_private"}
                groups[("raw", r["id"])].append(r)
                continue
            m = mchk.match(r["name_raw"], self.register)
            matches[r["id"]] = m
            if m["status"] == "matched":
                groups[("reg", m["entry"]["reg_no"])].append(r)
            else:
                # Not confidently identified: fall back to normalised-name grouping so obvious repeats still merge.
                groups[("name", names.key(r["name_raw"]))].append(r)

        # Second pass: a 'name' group that fuzzily equals a registered group is the same doctor.
        reg_groups = {k: v for k, v in groups.items() if k[0] == "reg"}
        for k in [k for k in groups if k[0] == "name"]:
            for rk, rv in reg_groups.items():
                if names.score(groups[k][0]["name_raw"], rv[0]["name_raw"]) >= config.NAME_MATCH_ACCEPT:
                    rv.extend(groups.pop(k))
                    break

        with db.tx(self.conn):
            for gkey, recs in groups.items():
                first = recs[0]
                m = matches[first["id"]]
                entry = m.get("entry") if m.get("status") == "matched" else None
                # Show the fullest name the hospitals publish (e.g. "Benjamin CHOW Kai Pun" over "Chow Kai Pun").
                shown = max((r["name_raw"] for r in recs), key=lambda n: len(names.tokens(n)))
                lead = {
                    "run_id": self.run_id,
                    "name": self._display_name(shown),
                    "name_key": names.key(entry["name"] if entry else first["name_raw"]),
                    "mchk_reg_no": entry["reg_no"] if entry else None,
                    "mchk_name": entry["name"] if entry else None,
                    "mchk_address": entry["address"] if entry else None,
                    "specialty": "Orthopaedics & Traumatology" if entry else first["specialty_raw"],
                    "hospitals": sorted({r["hospital"] for r in recs}),
                    "location": "Hong Kong",
                    "profile_urls": sorted({r["source_url"] for r in recs if r["source_url"]}),
                    "stage": "registry_checked",
                    "reason_codes": [], "notes": [],
                    "updated_at": db.now(),
                }
                if len(recs) > 1:
                    variants = "; ".join(f"'{r['name_raw']}' ({r['source']})" for r in recs)
                    lead["notes"].append(f"Merged {len(recs)} records for the same doctor: {variants}")
                if entry:
                    lead["notes"].append(f"MCHK register: {entry['reg_no']} {entry['name']} - {entry['qualification'].split(' ', 1)[-1] if entry['qualification'] else ''} (match score {m['score']})")
                    if any(not verify.specialty_matches(r["specialty_raw"]) for r in recs):
                        self._add_reason(lead, "SPECIALTY_CONFLICT", "Hospital listing gives a different specialty from the register: "
                                         + ", ".join(r["specialty_raw"] for r in recs))
                elif m.get("status") == "not_private":
                    self._add_reason(lead, "NOT_PRIVATE_HOSPITAL", f"'{first['hospital']}' is not one of the 13 HK private hospitals")
                elif m.get("status") in ("ambiguous", "possible"):
                    alts = ", ".join(f"{a['reg_no']} {a['name']}" for a in [{"reg_no": m['entry']['reg_no'], "name": m['entry']['name']}] + m["alternatives"])
                    self._add_reason(lead, "REGISTER_MATCH_UNSURE", f"Register match unsure (score {m['score']}): candidates {alts}")
                else:
                    self._add_reason(lead, "NOT_IN_REGISTER", f"'{first['name_raw']}' not found among {len(self.register)} registered orthopaedic specialists")
                if any(meta.get(r["name_raw"] + "|" + r["source"], {}).get("stale") for r in recs):
                    self._add_reason(lead, "SOURCE_STALE")

                crm = self._crm_match(lead)
                if crm:
                    self._add_reason(lead, "EXISTING_IN_CRM", f"Already in CRM as '{crm['name']}' (added {crm['added_on']})")

                lead["lead_status"] = verify.outcome(lead["reason_codes"]) if lead["reason_codes"] else "PENDING"
                cols = list(lead)
                vals = [db.jdump(lead[c]) if c in ("hospitals", "profile_urls", "reason_codes", "notes") else lead[c] for c in cols]
                cur = self.conn.execute(f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
                for r in recs:
                    self.conn.execute("UPDATE raw_records SET lead_id=? WHERE id=?", (cur.lastrowid, r["id"]))
        n_leads = self.conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=?", (self.run_id,)).fetchone()[0]
        self.log("deduplicated", f"{len(raws)} raw records -> {n_leads} unique doctors")

    @staticmethod
    def _display_name(raw: str) -> str:
        import re
        cleaned = re.sub(r"^\s*(dr|prof|professor)\.?\s+", "", raw.strip(), flags=re.I)
        return "Dr " + " ".join(cleaned.split())

    def _crm_match(self, lead):
        if lead["mchk_reg_no"]:
            row = self.conn.execute("SELECT * FROM crm_doctors WHERE mchk_reg_no=?", (lead["mchk_reg_no"],)).fetchone()
            if row:
                return dict(row)
        for row in self.conn.execute("SELECT * FROM crm_doctors"):
            if names.score(row["name"], lead["mchk_name"] or lead["name"]) >= config.NAME_MATCH_ACCEPT:
                return dict(row)
        return None

    # ------------------------------------------------------------------ stage 3: enrich (contact research)
    def enrich(self, crash_after: int | None = None):
        target = self.criteria.get("count", 20)
        rows = self.conn.execute(
            "SELECT id FROM leads WHERE run_id=? AND stage='registry_checked' AND lead_status IN ('PENDING','NEEDS_REVIEW','PENDING_ENRICHMENT') ORDER BY id",
            (self.run_id,)).fetchall()
        done = 0
        for (lead_id,) in rows:
            qualified = self.conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=? AND lead_status='QUALIFIED'", (self.run_id,)).fetchone()[0]
            if qualified >= target:
                self.log("target_reached", f"{qualified} qualified leads; skipping research for the rest")
                break
            lead = self._lead(lead_id)
            if "REGISTER_MATCH_UNSURE" in lead["reason_codes"] or "NOT_IN_REGISTER" in lead["reason_codes"]:
                self._save(lead, stage="done")
                continue
            if "PENDING_ENRICHMENT" in lead["reason_codes"]:
                lead["reason_codes"].remove("PENDING_ENRICHMENT")
            try:
                res = self.research(lead, self.budget)
            except llm.ApiLimitReached as e:
                self.log("api_limit", f"{e}. Remaining leads parked as PENDING_ENRICHMENT.", level="warn")
                for (rest_id,) in self.conn.execute(
                        "SELECT id FROM leads WHERE run_id=? AND stage='registry_checked' AND lead_status IN ('PENDING','PENDING_ENRICHMENT','NEEDS_REVIEW')",
                        (self.run_id,)).fetchall():
                    rest = self._lead(rest_id)
                    if "REGISTER_MATCH_UNSURE" in rest["reason_codes"]:
                        continue
                    if "PENDING_ENRICHMENT" not in rest["reason_codes"]:
                        self._add_reason(rest, "PENDING_ENRICHMENT")
                    self._save(rest, lead_status=verify.outcome(rest["reason_codes"]))
                return
            if res.get("found") and res.get("email"):
                lead["notes"].append(f"Contact research ({res.get('confidence')} confidence): {res.get('practice_name') or 'practice'} "
                                     f"{res.get('practice_url') or ''}".strip())
            if res.get("notes"):
                lead["notes"].append("Research note: " + res["notes"])
            if res.get("specialty_on_source") and not verify.specialty_matches(res["specialty_on_source"]):
                self._add_reason(lead, "SPECIALTY_CONFLICT", f"Practice website says '{res['specialty_on_source']}', register says orthopaedics")
            self._save(lead, email=emailcheck.normalise(res.get("email")) if res.get("found") else None,
                       email_source_url=res.get("email_source_url") or None, stage="enriched")
            self.verify_lead(lead)
            done += 1
            self.log("enriched", f"{lead['name']}: {lead['email'] or 'no email'} -> {lead['lead_status']} ({res.get('_searches', 0)} searches)", lead_id)
            self.conn.execute("UPDATE runs SET web_searches_used=? WHERE id=?", (self.budget.used, self.run_id))
            self.conn.commit()
            if crash_after and done >= crash_after:
                raise SimulatedCrash(f"Simulated crash after {done} enrichments (run {self.run_id}). Resume with --resume {self.run_id}")

    # ------------------------------------------------------------------ stage 4: verify + apply requirements
    def verify_lead(self, lead: dict):
        source_html = None
        if lead.get("email") and lead.get("email_source_url"):
            try:
                source_html = self.fetch(lead["email_source_url"]).html
            except SourceUnavailable as e:
                lead["notes"].append(f"Could not open email source page: {e}")
        v = emailcheck.verify(lead.get("email"), lead.get("mchk_name") or lead["name"], source_html)
        lead["notes"].extend(v["notes"])
        status = v["status"]
        code = {"not_found": "NO_EMAIL", "invalid": "INVALID_EMAIL", "excluded_domain": "EXCLUDED_EMAIL_DOMAIN",
                "unverified": "EMAIL_UNVERIFIED", "risky": "EMAIL_RISKY"}.get(status)
        if code:
            self._add_reason(lead, code, v["notes"][0] if v["notes"] else None)
        if status == "excluded_domain":
            lead["notes"].append(f"Excluded address removed: {v['email']}")
            v["email"] = None
        if v["email"]:
            crm = self.conn.execute("SELECT name FROM crm_doctors WHERE lower(email)=?", (v["email"],)).fetchone()
            if crm and "EXISTING_IN_CRM" not in lead["reason_codes"]:
                self._add_reason(lead, "EMAIL_IN_CRM", f"Email already in CRM for '{crm['name']}'")
        self._save(lead, email=v["email"], email_status=status, email_type=v["type"], stage="verified")
        lead["lead_status"] = verify.outcome(lead["reason_codes"])
        self._save(lead)

    # ------------------------------------------------------------------ stage 5: cross-lead checks + quality
    def quality_check(self):
        leads = [self._lead(r["id"]) for r in self.conn.execute("SELECT id FROM leads WHERE run_id=?", (self.run_id,))]
        by_email = defaultdict(list)
        for l in leads:
            if l["email"]:
                by_email[l["email"]].append(l)
        for email, group in by_email.items():
            if len(group) > 1:
                others = ", ".join(g["name"] for g in group)
                for l in group:
                    self._add_reason(l, "SHARED_EMAIL", f"{email} is shared by: {others} (likely a group-practice inbox)")
                    l["lead_status"] = verify.outcome(l["reason_codes"])
                    self._save(l)
        for l in leads:
            l = self._lead(l["id"])
            if l["lead_status"] == "PENDING" and l["stage"] == "registry_checked":
                continue  # not researched (target reached); stays PENDING
            if l["lead_status"] == "QUALIFIED":
                missing = verify.required_fields_present(l)
                if missing:
                    self._add_reason(l, "MISSING_FIELDS", "Missing: " + ", ".join(missing))
                    l["lead_status"] = verify.outcome(l["reason_codes"])
            self._save(l, stage="done" if l["stage"] == "verified" else l["stage"])

    def finish(self):
        counts = dict(self.conn.execute("SELECT lead_status, COUNT(*) FROM leads WHERE run_id=? GROUP BY lead_status", (self.run_id,)).fetchall())
        self.conn.execute("UPDATE runs SET status='completed', finished_at=?, web_searches_used=? WHERE id=?",
                          (db.now(), self.budget.used, self.run_id))
        self.conn.commit()
        paths = export.export_run(self.conn, self.run_id, self.criteria.get("count", 20))
        self.log("run_completed", f"Run {self.run_id} finished: {counts}. Files: {', '.join(str(p.name) for p in paths)}")
        return counts

    def run(self, request=None, resume_id=None, crash_after=None):
        if resume_id:
            self.resume(resume_id)
        else:
            self.start(request or config.DEFAULT_REQUEST)
        try:
            self.discover()
            self.identify_and_dedupe()
            self.enrich(crash_after=crash_after)
            self.quality_check()
            return self.finish()
        except SimulatedCrash:
            self.conn.execute("UPDATE runs SET status='failed' WHERE id=?", (self.run_id,))
            self.conn.commit()
            raise


def load_crm(conn, crm_csv=None, history_csv=None):
    """Load the mock 'existing database' (CRM + outreach history) once."""
    crm_csv = crm_csv or config.DATA_DIR / "crm_existing.csv"
    history_csv = history_csv or config.DATA_DIR / "outreach_history.csv"
    if conn.execute("SELECT COUNT(*) FROM crm_doctors").fetchone()[0] == 0 and crm_csv.exists():
        with open(crm_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                conn.execute("INSERT INTO crm_doctors(name, name_key, mchk_reg_no, email, hospital, added_on) VALUES (?,?,?,?,?,?)",
                             (r["name"], names.key(r["name"]), r.get("mchk_reg_no") or None, (r.get("email") or "").lower() or None,
                              r.get("hospital"), r.get("added_on")))
    if conn.execute("SELECT COUNT(*) FROM outreach_history").fetchone()[0] == 0 and history_csv.exists():
        with open(history_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                conn.execute("INSERT INTO outreach_history(email, mchk_reg_no, contacted_on, campaign) VALUES (?,?,?,?)",
                             (r["email"].lower(), r.get("mchk_reg_no") or None, r["contacted_on"], r.get("campaign")))
    conn.commit()


def main(argv=None):
    p = argparse.ArgumentParser(description="Doctor lead research pipeline")
    p.add_argument("--request", default=None)
    p.add_argument("--resume", type=int, default=None)
    p.add_argument("--crash-after", type=int, default=None)
    a = p.parse_args(argv)
    conn = db.connect()
    load_crm(conn)
    print(f"Mode: {'OFFLINE (no API key)' if config.OFFLINE else 'Claude ' + config.CLAUDE_MODEL}")
    try:
        Pipeline(conn).run(a.request, a.resume, a.crash_after)
    except SimulatedCrash as e:
        print(f"\nX {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
