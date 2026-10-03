# Doctor Lead Research & Outreach Automation (prototype)

Finds private-hospital orthopaedic doctors in Hong Kong, verifies them against the Medical Council register,
researches a public practice email with Claude, verifies the email, deduplicates, and runs a human-approved
outreach workflow. Full write-up: **[DOCUMENTATION.md](DOCUMENTATION.md)**.

## Setup (Windows, Python 3.12)

```bash
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # then put your ANTHROPIC_API_KEY in .env
```

Without an API key everything still runs in **offline mode** (no email research; template emails).

## Run

```bash
.venv\Scripts\python -m leadgen.pipeline                 # research run for the brief's request
.venv\Scripts\python -m leadgen.pipeline --request "Find 10 private-hospital orthopedic doctors in Hong Kong"
.venv\Scripts\python -m leadgen.pipeline --crash-after 3 # demo: simulate a crash...
.venv\Scripts\python -m leadgen.pipeline --resume 2      # ...and resume the same run
.venv\Scripts\python -m leadgen.outreach prepare         # checks + AI drafts for qualified leads
.venv\Scripts\python app.py                              # reviewer UI at http://127.0.0.1:5000
.venv\Scripts\python run_edge_cases.py                   # the 14 edge cases, offline, PASS/FAIL table
```

## Outputs

| File | What |
|---|---|
| `output/runN_qualified_leads.csv` | Final list (up to the requested count) |
| `output/runN_all_leads.csv` | Every candidate with status, reason codes and notes |
| `output/runN_leads.xlsx` | Both lists, colour-coded by status |
| `output/leads.db` | SQLite database (runs, raw records, leads, outreach, event log) |
| `outbox/*.eml` | Approved emails in simulate mode |
| `output/edge_cases_report.txt` | Edge-case harness results |

## Safety

- Nothing is emailed without a person clicking **Approve** and then **Send**.
- Default sending mode writes `.eml` files; SMTP mode requires `TEST_RECIPIENT_OVERRIDE`, so a demo never reaches a real doctor.
- Web research is capped per run (`MAX_WEB_SEARCHES_PER_RUN`).
