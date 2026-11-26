# Vendor Onboarding Agent — PS-2

Takes a vendor submission (form fields + PDF documents), runs it through 7 verification stages,
and returns **approved / pending / rejected** with visible reasoning, a message back to the vendor,
and an internal audit note.

## Run locally
```bash
pip install -r requirements.txt
python make_samples.py          # builds 6 test submissions (JSON + PDFs) in ./samples
uvicorn app:app --reload        # open http://localhost:8000
```
Optional: `export ANTHROPIC_API_KEY=...` to have Claude polish the wording of vendor emails.
Every decision stays rule-based. The LLM only rewrites the email text.

Reset before a demo: `curl -X POST http://localhost:8000/api/reset`

## Pipeline
| # | Stage | What it checks |
|---|-------|----------------|
| 1 | Intake & completeness | Required fields per country, all 3 documents present |
| 2 | Document verification | PDF is readable, content matches the declared type (catches an invoice uploaded as a GST cert), tax ID and account number on form appear in the documents |
| 3 | Tax ID validation | GSTIN (format + state code + checksum), PAN, US EIN (format + IRS-valid prefix), UK VAT (mod-97); catches a tax ID from the wrong country |
| 4 | Bank verification | Account-holder name vs legal name (fuzzy, ignores Pvt/Ltd/LLC), IFSC / ABA checksum / sort code, account length, bank country |
| 5 | Cross-field consistency | GSTIN state vs address state, PAN inside GSTIN vs PAN field, postal-code format, phone prefix, email domain vs website |
| 6 | Fraud & risk | Bank account or tax ID already used by a *different* vendor, watchlist name match |
| 7 | Decision | any `block` → rejected · any `fix` → pending · else approved |

Severity model: `pass` · `warn` (FYI, never blocks) · `fix` (vendor can correct → pending) · `block` (fraud/compliance → rejected).

## AI + data privacy
- **Bank documents never leave the server.** They are parsed with local rules only (`LOCAL_ONLY_DOCS=bank_proof`).
- **Other documents:** text is extracted locally, then account numbers, IBANs, GSTIN/PAN/EIN/VAT, IFSC/sort codes,
  phones and emails are replaced with tokens like `[TAX_ID_1]` (`privacy.py`). Only that redacted text goes to Claude.
  Claude returns tokens, and the real values are put back locally.
- **Scans are never sent as images.** Unreadable files go back to the vendor / a human.
- **AI extracts and flags. It never decides.** Matching tax IDs and account numbers is done locally by rules.
- **No raw account numbers are stored** in runs or the vendor registry: only a keyed hash (HMAC-SHA256) for
  duplicate detection, plus the last 4 digits for display. The raw number sits in one `run_secrets` table
  (encrypt with a KMS in production), is never returned by the API, and uploaded files are deleted after each run.
- The UI shows exactly **what the AI saw** for every document.
- Without `ANTHROPIC_API_KEY` (or if the API fails) the app falls back to local extraction, so it always runs.

## Tests
`python -m pytest -q`: validators, redaction (no identifier reaches the LLM), all 7 sample cases end-to-end,
no raw account numbers in the database, and rejection emails that don't reveal the fraud signal.

## Design decisions
- **Rules decide, AI words.** Approval decisions have to be explainable and repeatable for audit, so they are deterministic. The LLM is only used for wording.
- **Rejection messages are deliberately vague.** Telling a fraudster "your bank account belongs to another vendor" teaches them what to change. The specific reason goes in the internal note only.
- **Name mismatch → pending, not rejected.** Trade names and parent-company accounts are legitimate. The fix is a bank letter, so this is a request, not a refusal.
- **A crashing rule can never approve.** Stage exceptions become `fix` items.
- **Approved vendors join the vendor master,** so later submissions are checked against them for duplicate accounts and tax IDs.

## Test cases (in ./samples)
1. Happy path, India → approved
2. Happy path, US → approved
3. Incomplete form + invoice uploaded as GST certificate + missing bank proof → pending
4. UK: bank account in the director's personal name + VAT checksum fails + Gmail contact → pending
5. India: every field valid on its own, but the GSTIN is for Karnataka while the address is in Maharashtra, and the PAN differs by one character → pending
6. Clean-looking vendor whose bank account already belongs to an existing vendor → rejected
7. Incorporation certificate issued to the parent company, not the vendor → pending

## API
`POST /api/submissions` (multipart: `payload` JSON + `tax_certificate`, `bank_proof`, `incorporation_certificate` PDFs) ·
`GET /api/runs` · `GET /api/runs/{id}` · `POST /api/samples/{name}/run` · `POST /api/reset`
