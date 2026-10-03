"""Edge-case harness: pushes the 14 situations from the brief through the REAL pipeline code.

All doctors here are FICTIONAL (register numbers S08-9xxx, domains *-fixture.hk) so no real person is
attached to invented data. Network calls are replaced by fixtures, so this runs offline in seconds and
gives the same result every time. It writes to output/edge_cases.db (the live DB is untouched).

    python run_edge_cases.py
"""
import sys
from pathlib import Path

from leadgen import config, db, emailcheck, llm, outreach, pipeline
from leadgen.http import Page, SourceUnavailable
from leadgen.sources import mchk

H = config.PRIVATE_HOSPITALS
DB = config.OUTPUT_DIR / "edge_cases.db"

# Fictional register entries added on top of the real MCHK orthopaedic register.
FIXTURE_REGISTER = [
    {"reg_no": "S08-9001", "name": "LAM, KA HO TERENCE", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9002", "name": "WONG, MEI YEE", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9003", "name": "TSANG, HOI LAM", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9004", "name": "HO, WING SZE", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9005", "name": "YUEN, KAI MING", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9006", "name": "LEUNG, CHI FAI", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9007", "name": "KWOK, SIU MAN", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9008", "name": "FONG, PUI SHAN", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9009", "name": "CHOI, MAN KIT", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9010", "name": "LO, YAN TING", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9011", "name": "SZETO, HO YIN", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
    {"reg_no": "S08-9012", "name": "MAN, TSZ KIN", "address": "", "specialty": "Orthopaedics & Traumatology", "qualification": "FHKAM(Orthopaedic Surgery)"},
]


def rec(name, hospital_key, specialty="Orthopaedics & Traumatology", hospital=None):
    return {"name": name, "specialty": specialty, "hospital_key": "fixture", "hospital": hospital or H[hospital_key],
            "profile_url": f"https://directory-fixture.hk/{name.lower().replace(' ', '-').replace('.', '')}",
            "source_url": "https://directory-fixture.hk/", "qualifications": "FHKAM (Orthopaedic Surgery)"}


# case -> (raw record(s), research result, expected lead_status, expected reason code)
RECORDS = [
    rec("Dr. Lam Ka Ho, Terence", "hkah"),                       # 0 control: fully qualified
    rec("Dr. Wong Mei Yee", "matilda"),                          # 1 already exists (in fixture CRM)
    rec("Dr. Notreal Fictional Ho", "stpaul"),                      # 2 does not exist (not in register)
    rec("Dr. Tsang Hoi Lam", "union"),                           # 3 duplicate doctor: same person, two sites,
    rec("Dr Hoi-Lam TSANG", "hkbh"),                             #   different name formats
    rec("Dr. Ho Wing Sze", "canossa"),                           # 4 duplicate email (shares with #4b)
    rec("Dr. Yuen Kai Ming", "canossa"),                         # 4b
    rec("Dr. Leung Chi Fai", "sth"),                             # 5 no email found
    rec("Dr. Kwok Siu Man", "evangel"),                          # 6 invalid email
    rec("Dr. Fong Pui Shan", "matilda", specialty="Cardiology"), # 7 wrong specialty (filtered at discovery)
    rec("Dr. Choi Man Kit", "hkah", hospital="Queen Mary Hospital"),  # 8 excluded (public) hospital
    rec("Dr. Lo Yan Ting", "gleneagles"),                        # 9 excluded email domain (hku.hk)
    rec("Dr. Szeto Ho Yin", "hksh"),                             # 12 AI gives an email not on the source page
    rec("Dr. Man Tsz Kin", "cuhkmc"),                            # 13 conflicting sources
]

RESEARCH = {
    "S08-9001": {"found": True, "email": "dr.lam@lam-ortho-fixture.hk", "email_source_url": "https://lam-ortho-fixture.hk/contact"},
    "S08-9003": {"found": True, "email": "tsang@tsang-spine-fixture.hk", "email_source_url": "https://tsang-spine-fixture.hk/"},
    "S08-9004": {"found": True, "email": "info@harbour-ortho-group-fixture.hk", "email_source_url": "https://harbour-ortho-group-fixture.hk/team"},
    "S08-9005": {"found": True, "email": "info@harbour-ortho-group-fixture.hk", "email_source_url": "https://harbour-ortho-group-fixture.hk/team"},
    "S08-9006": {"found": False, "email": "", "email_source_url": "", "notes": "No practice website or public email found"},
    "S08-9007": {"found": True, "email": "kwok@kwok-ortho-gone-fixture.zz", "email_source_url": "https://kwok-fixture.hk/"},
    "S08-9010": {"found": True, "email": "ytlo@hku.hk", "email_source_url": "https://hku-fixture.hk/staff/lo"},
    "S08-9011": {"found": True, "email": "szeto@szeto-clinic-fixture.hk", "email_source_url": "https://szeto-clinic-fixture.hk/"},
    "S08-9012": {"found": True, "email": "drman@man-ortho-fixture.hk", "email_source_url": "https://man-ortho-fixture.hk/",
                 "specialty_on_source": "Plastic & Reconstructive Surgery", "notes": "Practice site lists plastic surgery"},
}

PAGES = {
    "https://lam-ortho-fixture.hk/contact": "<h1>Dr Lam Ka Ho Terence</h1><p>Orthopaedic surgeon</p><a href='mailto:dr.lam@lam-ortho-fixture.hk'>email</a>",
    "https://tsang-spine-fixture.hk/": "<h1>Dr Tsang Hoi Lam - Spine</h1><p>tsang@tsang-spine-fixture.hk</p>",
    "https://harbour-ortho-group-fixture.hk/team": "<p>Dr Ho Wing Sze, Dr Yuen Kai Ming</p><p>Enquiries: info@harbour-ortho-group-fixture.hk</p>",
    "https://kwok-fixture.hk/": "<p>Dr Kwok Siu Man</p><p>kwok@kwok-ortho-gone-fixture.zz</p>",
    "https://hku-fixture.hk/staff/lo": "<p>Dr Lo Yan Ting</p><p>ytlo@hku.hk</p>",
    # Szeto's page exists but does NOT contain the address Claude reported -> must be caught.
    "https://szeto-clinic-fixture.hk/": "<h1>Dr Szeto Ho Yin</h1><p>Call +852 0000 0000 to book.</p>",
    "https://man-ortho-fixture.hk/": "<h1>Dr Man Tsz Kin</h1><p>Plastic & Reconstructive Surgery</p><p>drman@man-ortho-fixture.hk</p>",
}

EXPECTED = [
    ("0  Control (everything valid)",            "Dr Lam Ka Ho, Terence", "QUALIFIED", None),
    ("1  Doctor already exists (CRM)",           "Dr Wong Mei Yee", "EXISTING_IN_CRM", "EXISTING_IN_CRM"),
    ("2  Doctor does not exist",                 "Dr Notreal Fictional Ho", "REJECTED", "NOT_IN_REGISTER"),
    ("3  Duplicate doctor (2 sites, 2 formats)", "Dr Tsang Hoi Lam", "QUALIFIED", None),
    ("4  Duplicate email (shared inbox)",        "Dr Ho Wing Sze", "NEEDS_REVIEW", "SHARED_EMAIL"),
    ("5  No email found",                        "Dr Leung Chi Fai", "NEEDS_REVIEW", "NO_EMAIL"),
    ("6  Invalid email (domain cannot receive)", "Dr Kwok Siu Man", "NEEDS_REVIEW", "INVALID_EMAIL"),
    ("7  Wrong specialty",                       "Dr Fong Pui Shan", None, None),
    ("8  Excluded hospital (public)",            "Dr Choi Man Kit", "REJECTED", "NOT_PRIVATE_HOSPITAL"),
    ("9  Excluded email domain (HKU)",           "Dr Lo Yan Ting", "REJECTED", "EXCLUDED_EMAIL_DOMAIN"),
    ("12 AI reports an email not on the page",   "Dr Szeto Ho Yin", "NEEDS_REVIEW", "EMAIL_UNVERIFIED"),
    ("13 Two sources conflict",                  "Dr Man Tsz Kin", "NEEDS_REVIEW", "SPECIALTY_CONFLICT"),
]


def fake_fetch(url, **_):
    if url in PAGES:
        return Page(url, PAGES[url], db.now(), from_cache=False, stale=False)
    raise SourceUnavailable(f"{url}: connection failed")


class FakeResearch:
    """Stands in for Claude web research. Raises ApiLimitReached on call number `fail_on` (case 11)."""
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = 0, fail_on

    def __call__(self, lead, budget):
        self.calls += 1
        if self.fail_on and self.calls == self.fail_on:
            raise llm.ApiLimitReached("simulated 429 rate limit (case 11)")
        base = {"found": False, "email": "", "email_source_url": "", "confidence": "high", "practice_name": "",
                "practice_url": "", "specialty_on_source": "", "hospitals_on_source": [], "evidence_quote": "", "notes": "", "_searches": 1}
        base.update(RESEARCH.get(lead["mchk_reg_no"], {}))
        return base


def fake_mx(domain):
    if domain.endswith("-fixture.hk"):
        return True, "MX: mx.fixture (mocked)"
    if domain.endswith(".zz"):
        return False, "domain does not exist"
    return emailcheck._real_mx(domain)


def setup():
    if DB.exists():
        DB.unlink()
    conn = db.connect(DB)
    conn.execute("INSERT INTO crm_doctors(name, name_key, mchk_reg_no, email, hospital, added_on) VALUES (?,?,?,?,?,?)",
                 ("Dr Mei-Yee Wong", "mei wong yee", None, None, H["matilda"], "2026-04-01"))
    conn.commit()
    emailcheck._real_mx = emailcheck.domain_accepts_mail
    emailcheck.domain_accepts_mail = fake_mx
    return conn


def make_pipeline(conn, research):
    register = mchk.load_register() + FIXTURE_REGISTER
    sources = [{"key": "stpaul", "parser": "hkah", "url": "https://hospital-directory.invalid/doctors"}]  # case 10
    return pipeline.Pipeline(conn, research_fn=research, fetch_fn=fake_fetch, register=register,
                             sources=sources, extra_records=[dict(r) for r in RECORDS])


def banner(text):
    print("\n" + "=" * 100 + "\n" + text + "\n" + "=" * 100)


def main():
    config.OFFLINE = True          # request parsing by rules; no API calls in the harness
    config.EXPORT_PREFIX = "edge_cases_"
    conn = setup()

    banner("PASS 1 - case 11: the Claude API hits its rate limit on the 6th research call")
    p = make_pipeline(conn, FakeResearch(fail_on=6))
    p.run(request=config.DEFAULT_REQUEST)
    run_id = p.run_id
    parked = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=? AND lead_status='PENDING_ENRICHMENT'", (run_id,)).fetchone()[0]
    leads_before = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=?", (run_id,)).fetchone()[0]
    print(f"\n-> {parked} leads parked as PENDING_ENRICHMENT; nothing lost")

    banner("PASS 2 - case 14: resume, but the process crashes after 1 more lead")
    crashed = False
    try:
        make_pipeline(conn, FakeResearch()).run(resume_id=run_id, crash_after=1)
    except pipeline.SimulatedCrash as e:
        crashed = True
        print(f"\n-> {e}")

    banner("PASS 3 - resume again: only the unfinished leads are processed")
    make_pipeline(conn, FakeResearch()).run(resume_id=run_id)
    n_leads = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=?", (run_id,)).fetchone()[0]
    still_parked = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id=? AND lead_status='PENDING_ENRICHMENT'", (run_id,)).fetchone()[0]
    print(f"\n-> {n_leads} leads before and after resuming ({leads_before} at start); {still_parked} still parked")

    print("\n" + "=" * 100)
    print("RESULTS")
    print("=" * 100)
    leads = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM leads WHERE run_id=?", (run_id,))}
    fails = 0
    for case, name, exp_status, exp_code in EXPECTED:
        lead = leads.get(name)
        if exp_status is None:
            ok = lead is None
            got = "filtered out at discovery" if lead is None else lead["lead_status"]
        else:
            codes = db.jload(lead["reason_codes"]) if lead else []
            ok = bool(lead) and lead["lead_status"] == exp_status and (exp_code is None or exp_code in codes)
            got = f"{lead['lead_status']} {codes}" if lead else "MISSING"
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {case:42} -> {got}")
    dup = leads.get("Dr Tsang Hoi Lam")
    print(f"{'PASS' if dup and len(db.jload(dup['hospitals'])) == 2 else 'FAIL'}  3b Duplicate merged into one lead with both hospitals -> {db.jload(dup['hospitals']) if dup else None}")
    src = conn.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND code='source_unavailable'", (run_id,)).fetchone()[0]
    print(f"{'PASS' if src else 'FAIL'}  10 Source website unavailable -> logged {src} time(s), run continued")
    ok11 = parked > 0 and still_parked == 0
    ok14 = crashed and n_leads == leads_before
    fails += (not ok11) + (not ok14) + (not src)
    print(f"{'PASS' if ok11 else 'FAIL'}  11 API limit reached -> {parked} leads parked, all finished after resume")
    print(f"{'PASS' if ok14 else 'FAIL'}  14 Automation fails halfway -> crashed, resumed, {n_leads} leads (no duplicates)")

    print("\n" + "=" * 100)
    print("Case 12b: AI-drafted email that invents facts is caught by the fact check before approval")
    print("=" * 100)
    lead = dict(conn.execute("SELECT * FROM leads WHERE name='Dr Lam Ka Ho, Terence'").fetchone())
    facts = outreach.fact_sheet(lead)
    bad = {"subject": "Following up", "body": "Dear Dr Lam,\nAs we discussed at the 2025 conference, your recent paper on ACL repair "
           "was brilliant. We'd love you to speak at Gleneagles Hospital. Call us on +852 5555 1234.\n\n" + facts["signature"]}
    for problem in outreach.fact_check(bad, facts):
        print("  caught:", problem)
    good = llm.template_email(facts)
    print("  template draft problems:", outreach.fact_check(good, facts) or "none")

    print(f"\nDatabase: {DB}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
