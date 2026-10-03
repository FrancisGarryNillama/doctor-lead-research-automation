# Doctor Lead Research & Outreach Automation — Documentation

Prototype for the Medic Paths *IT & AI Automation Specialist* practical assessment.
Author: Francis Garry Nillama · Python 3.12 · Claude API · SQLite · Flask

---

## 1. Architecture

```
                         ┌────────────────────────────── pipeline.py (CLI, resumable) ──────────────────────────────┐
 "Find 20 private-       │                                                                                            │
  hospital orthopedic ──►│ 0 Request      Claude → structured criteria (count, specialty, location, setting, exclusions)│
  doctors in HK…"        │ 1 Search       Hospital directories (HKAH, Matilda) → raw doctor records, specialty filter  │
                         │ 2 Identify     Match every record to the Medical Council (MCHK) specialist register          │
                         │   Deduplicate  Merge by register no. → fuzzy name; check existing CRM                        │
                         │ 3 Enrich       Claude web search + web fetch → practice website + public email + source URL │
                         │ 4 Verify       Syntax · HKU exclusion · MX record · email on source page · page names doctor │
                         │   Rules        Reason codes → QUALIFIED / NEEDS_REVIEW / REJECTED / EXISTING_IN_CRM          │
                         │ 5 Quality      Shared-email check · required fields · export CSV + Excel                     │
                         └───────────────────────────────────────────────┬────────────────────────────────────────────┘
                                                                         │ SQLite (output/leads.db)
                         ┌───────────────────────── outreach.py + app.py (web UI) ─────────────────────────────────────┐
                         │ Qualified → email available? → verified? → previously contacted? → Claude draft + fact check│
                         │ → HUMAN APPROVAL (edit / approve / reject) → send (simulated .eml or SMTP to test inbox)     │
                         │ → log (outreach table + outreach_history + event log)                                        │
                         └─────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Component | File | Job |
|---|---|---|
| Configuration and business rules | `leadgen/config.py` | Private hospital list, excluded domains, thresholds, campaign brief |
| Storage | `leadgen/db.py` | SQLite schema: runs, raw_records, leads, outreach, events, CRM mock |
| Fetching | `leadgen/http.py` | Retries with backoff, disk cache, stale-cache fallback |
| Sources | `leadgen/sources/hospitals.py`, `mchk.py` | Hospital directory parsers; MCHK register download and matching |
| Name matching | `leadgen/names.py` | Order-independent token matching for Hong Kong name formats |
| AI | `leadgen/llm.py` | Request parsing, contact research, email drafting (Claude) |
| Email checks | `leadgen/emailcheck.py` | Syntax, exclusion, DNS, on-source, association |
| Rules | `leadgen/verify.py` | Reason codes and how they decide a lead's status |
| Orchestration | `leadgen/pipeline.py` | Stages, checkpoints, resume, crash simulation |
| Outreach | `leadgen/outreach.py` | Eligibility checks, drafting, fact check, approval, send, log |
| Reviewer UI | `app.py`, `templates/` | Dashboard, leads, outreach approval, event log |
| Edge-case harness | `run_edge_cases.py` | All 14 brief scenarios through the real code, offline |

## 2. Tools — and why

| Tool | Why |
|---|---|
| **Python** | Full control over matching, verification and error handling; easy to test; no per-run platform cost. An n8n/Make version would orchestrate the same steps, but the dedupe and verification logic would still live in code nodes. |
| **Claude API (`claude-opus-5-5`)** | Web research needs judgment (which page is the doctor's own practice, is this address theirs). Claude's server-side **web search** and **web fetch** tools do the searching without a separate search API. Structured outputs / a strict tool schema return machine-readable results. |
| **MCHK specialist register** | The official source for "is this a registered doctor" and "is the specialty right". |
| **SQLite** | One file, zero setup, transactions for safe checkpoints. Swap for Postgres in production (same SQL). |
| **Flask** | Small reviewer UI for the human-approval step. |
| **dnspython, rapidfuzz, BeautifulSoup, openpyxl** | MX checks, fuzzy name matching, HTML parsing, Excel export. |

## 3. Data sources and reliability

| Source | What it gives | Reliability |
|---|---|---|
| **Medical Council of Hong Kong — Specialist Register** (orthopaedics filter, 556 doctors) | Registration no., registered name, specialty, qualification, sometimes address | Authoritative and official. Used as the source of truth for identity and specialty. |
| **Hong Kong Adventist Hospital** — Find a Doctor | Doctors with privileges, specialty, profile URL | High (hospital's own directory). Proves "works at a private hospital". |
| **Matilda International Hospital** — Find a Doctor | Same, with specialty tags | High. |
| **Doctors' own practice / clinic websites** (found by Claude web search) | Public practice email | Medium. Varies by site; this is why every address is re-checked on its source page. |
| **Mock CRM + outreach history** (`data/*.csv`) | "Already in our database", "previously contacted" | Stand-in for the company CRM. |

Hospital pages publish phone numbers and booking forms but **not** doctors' personal emails, so emails come from
practice websites. Many private specialists list only a clinic inbox (`info@…`); those are kept and marked
`clinic_generic` so the team can decide whether a clinic inbox is acceptable.

## 4. Data quality rules

A lead is **QUALIFIED** only if *every* check passes. Uncertain → **NEEDS_REVIEW** (never silently accepted).

| Question from the brief | How it is checked | If it fails |
|---|---|---|
| Is the person actually a doctor? | Name matched to the MCHK specialist register | `NOT_IN_REGISTER` → REJECTED; partial/multiple match → `REGISTER_MATCH_UNSURE` → NEEDS_REVIEW |
| Does the specialty match? | Register entry is orthopaedics; hospital and website specialty compared | `WRONG_SPECIALTY` → REJECTED; disagreement → `SPECIALTY_CONFLICT` → NEEDS_REVIEW |
| Eligible private hospital? | Listed by one of the 13 HK private hospitals | `NOT_PRIVATE_HOSPITAL` → REJECTED |
| Correct location? | Source is a Hong Kong hospital and HK register | `OUTSIDE_LOCATION` → REJECTED |
| Is the email valid? | Syntax + domain has MX/A records | `INVALID_EMAIL` → NEEDS_REVIEW |
| Is the email associated with the doctor? | Address appears on the cited page **and** that page names the doctor (or the address contains their name) | `EMAIL_UNVERIFIED` / `EMAIL_RISKY` → NEEDS_REVIEW |
| Is the domain excluded? | `hku.hk` and every subdomain, `hku-szh.org`, … | `EXCLUDED_EMAIL_DOMAIN` → REJECTED (address removed) |
| Already in the database? | CRM matched by register no., fuzzy name, or email | `EXISTING_IN_CRM` / `EMAIL_IN_CRM` |
| Already in the outreach list? | Outreach history by email or register no. within 180 days | Outreach skipped with reason |

### Deduplication logic

1. **Exact key first:** every record is matched to the MCHK register; records with the same registration number
   are the same doctor, whatever their spelling (`Dr. Chow Kai Pun` at HKAH = `Dr. Benjamin CHOW Kai Pun` at
   Matilda = `CHOW, KAI PUN` S08-0236).
2. **Normalised names:** titles, punctuation, hyphens and Chinese characters removed; tokens compared as a set, so
   name order does not matter (`Mok Wah Hong, Daniel` = `MOK, WAH HONG DANIEL` = `Wah-Hong Daniel Mok`).
3. **Fuzzy score:** `0.7 × token_set_ratio + 0.3 × token_sort_ratio`, surname must match. ≥90 = same person;
   75–90 or two close candidates = human review (e.g. the register has a `CHAN, HO`, so `Chan … Ho` is flagged, not merged).
4. **Same email, different doctors:** both flagged `SHARED_EMAIL` (usually a group-practice inbox).
5. **Existing database:** register number, then fuzzy name, then email against the CRM.

Merged records keep their audit trail: every source record is stored in `raw_records` and linked to its lead.

## 5. AI usage and guardrails

| Step | AI does | Guardrail so a wrong answer cannot reach the final list |
|---|---|---|
| Parse request | Natural-language request → JSON criteria (structured output) | Schema-validated; unsupported specialty stops the run; rule-based fallback |
| Contact research | Claude web search/fetch finds practice site + email | System prompt forbids guessed addresses. Code then **re-fetches the cited page itself** and confirms the address is on it and the page names the doctor. Anything else → NEEDS_REVIEW. |
| Draft email | Personalised email from a fact sheet | Model only gets a fact sheet. A deterministic **fact check** flags names, numbers, links, phone numbers, other hospitals or "as we discussed"-type claims not in the facts. **A person approves every email**; edits are re-checked. |

Identity, specialty and hospital eligibility are decided by code and official data, not by the model.
Web pages are treated as data: the research prompt tells Claude to ignore instructions inside pages, and nothing a
page says can change a lead's status without passing the code checks.
Requests use the API's server-side refusal fallback so a declined request is retried on a fallback model.

## 6. Error handling and edge cases

All 14 are exercised by `python run_edge_cases.py` (output in `output/edge_cases_report.txt`).

| # | Situation | Detect | Handle |
|---|---|---|---|
| 1 | Doctor already exists | Register no. / fuzzy name / email vs CRM | `EXISTING_IN_CRM`, not re-added, no research spent |
| 2 | Doctor does not exist | No register match | `NOT_IN_REGISTER` → REJECTED |
| 3 | Duplicate doctor | Same register no. or name score ≥90 | Merged into one lead; all hospitals + source records kept |
| 4 | Duplicate email | Same address on 2+ leads | `SHARED_EMAIL` → review; one outreach per address |
| 5 | No email found | Research returns none | `NO_EMAIL` → review queue |
| 6 | Invalid email | Syntax / no MX | `INVALID_EMAIL` → review |
| 7 | Wrong specialty | Directory specialty filter; register; website | Filtered out, or `SPECIALTY_CONFLICT` |
| 8 | Excluded hospital | Not in the 13 private hospitals | `NOT_PRIVATE_HOSPITAL` → REJECTED |
| 9 | Excluded email domain | Domain or subdomain of an HKU domain | Address removed; `EXCLUDED_EMAIL_DOMAIN` |
| 10 | Source website unavailable | HTTP error/timeout after 3 retries with backoff | Use last cached copy (flagged `SOURCE_STALE`) or skip the source and continue |
| 11 | API limit reached | `RateLimitError` after retries, or per-run search budget used | Remaining leads parked `PENDING_ENRICHMENT`; picked up by `--resume` |
| 12 | AI generates incorrect information | Email not on cited page; draft contains facts not in fact sheet | `EMAIL_UNVERIFIED`; draft `flagged` for human edit |
| 13 | Two sources conflict | Hospital vs register vs website specialty | Register wins, lead goes to review with both values in the notes |
| 14 | Automation fails halfway | Per-lead `stage` checkpoints committed after each lead | `--resume <run>` continues from the last completed lead; no duplicates |

Every decision is written to the `events` table (shown in the UI's Event log) and to the lead's verification notes.

## 7. Assumptions

- "Private-hospital orthopedic doctor" = MCHK-registered orthopaedic specialist listed by one of Hong Kong's 13 private hospitals.
- "HKU-related email domains" = `hku.hk` and all subdomains, plus HKU-Shenzhen Hospital's domain. The list is in config.
- A clinic/practice inbox counts as the doctor's email if the page that lists it names the doctor; it is labelled `clinic_generic`.
- The outreach purpose is assumed (Medic Paths is a medic-led education provider): inviting the doctor to give a short talk to students interested in medicine. It is one config entry.
- "Verified" means: valid syntax, domain accepts mail, address published on a page that names the doctor. No mailbox (SMTP) probing.
- The existing database and outreach history are mocked with CSV files.

## 8. Limitations

- Two hospital parsers in the prototype (HKAH, Matilda); other hospitals need a parser each (or an AI page extractor).
- Public emails for private specialists are scarce; many leads end in NEEDS_REVIEW with `NO_EMAIL`. That is the true state of the data, not hidden.
- Email verification stops at DNS + source page; deliverability is not guaranteed.
- Web research costs money per doctor; capped by `MAX_WEB_SEARCHES_PER_RUN`.

## 9. Production version (hundreds or thousands of leads)

- **Scale:** queue workers (e.g. Celery/RQ or a managed queue) per lead; the per-lead stage model already fits this. Use the Message Batches API for drafting (50% cheaper).
- **Sources:** parsers for all 13 private hospitals plus clinic-group directories; scheduled register refresh; change detection.
- **Email:** a verification service (ZeroBounce/NeverBounce) for SMTP-level checks and catch-all detection.
- **Data:** Postgres, CRM integration (HubSpot/Airtable) instead of CSV mocks; unique constraints on register no. and email.
- **Compliance:** Hong Kong PDPO direct-marketing rules — record the lawful basis, include an unsubscribe line, keep a suppression list checked before every send, store source URL and date for every address (already stored).
- **Sending:** real ESP (SES/SendGrid) with domain authentication (SPF/DKIM/DMARC), throttling, bounce and reply handling feeding back into lead status.
- **Quality loop:** track reviewer decisions and bounce rates; feed recurring corrections into rules and prompts; alerting on error spikes.

## 10. AI tools used to build this

- **Build tool:** Claude Code (an AI coding assistant) was used to research the data sources, write most of the code
  and draft this documentation, working in my terminal under my direction. I am stating this plainly because the
  brief asks for it and because this is how I work day to day.
- **My part:** I set the scope and the stack (Python, SQLite, a Flask reviewer app, the Claude API, a one-day build),
  and signed off the design rules: AI output is never trusted until code verifies it, uncertain leads go to a person, and no
  email is sent without human approval. I supplied the API key and ran the live pipeline against the real hospital
  directories and the Medical Council register, including the run that hit a real Claude overload (529) mid-way and
  was finished with `--resume`. I ran the 14 edge-case tests, checked the qualified and review-queue leads against
  their source pages, tested the reviewer app end to end (edit, re-check, approve, simulated send, log), and recorded
  the walkthrough. I can explain every stage of the pipeline and why it is built the way it is.
- **Runtime AI:** the system itself calls the Claude API for request parsing, contact research and email drafting (section 5).
