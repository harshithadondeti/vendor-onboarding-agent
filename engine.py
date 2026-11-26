"""Vendor onboarding pipeline.

Seven stages run in order. Each stage emits checks with a severity:
  pass   - fine
  warn   - worth a human glance, never blocks
  fix    - vendor must correct something  -> PENDING
  block  - fraud / compliance stop         -> REJECTED
"""
import json, os, re, sqlite3, time, uuid, urllib.request
from datetime import datetime, timezone
from pypdf import PdfReader
import validators as V, privacy, ai_extract

DB_PATH = os.environ.get("DB_PATH", "onboarding.db")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "uploads")
STAGE_DELAY = float(os.environ.get("STAGE_DELAY", "0.7"))  # makes the live view readable

REQUIRED_DOCS = {
    "tax_certificate": "Tax registration certificate",
    "bank_proof": "Bank proof (cancelled cheque / bank letter)",
    "incorporation_certificate": "Certificate of incorporation",
}

STAGES = [
    ("intake", "Intake & completeness"),
    ("documents", "Document verification"),
    ("tax", "Tax ID validation"),
    ("bank", "Bank account verification"),
    ("consistency", "Cross-field consistency"),
    ("risk", "Fraud & risk screening"),
    ("decision", "Decision & vendor message"),
]

# Demo watchlist (stand-in for a real sanctions API such as OFAC / UN lists)
WATCHLIST = ["Global Shadow Trading FZE", "Volkov Petrochem Holdings", "Red Crescent Exports LLC"]


# ---------------- DB ----------------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY, created_at TEXT, vendor_name TEXT, country TEXT,
            status TEXT, current_stage TEXT, payload TEXT, events TEXT, result TEXT,
            resubmission_of TEXT);
        -- Existing vendors: account stored only as a keyed hash (fingerprint) + last 4 digits.
        CREATE TABLE IF NOT EXISTS vendor_registry(
            id INTEGER PRIMARY KEY AUTOINCREMENT, legal_name TEXT, tax_id TEXT,
            account_fp TEXT, account_last4 TEXT, source TEXT);
        -- Raw bank numbers live only here: never returned by the API, never sent to AI.
        -- In production this table would be encrypted (KMS / vault).
        CREATE TABLE IF NOT EXISTS run_secrets(run_id TEXT PRIMARY KEY, account_number TEXT);
        """)
        if con.execute("SELECT COUNT(*) FROM vendor_registry").fetchone()[0] == 0:
            seed = [("Deccan Cargo Movers Private Limited", "36AABCD4321K1Z0", "50100277881234"),
                    ("Patel Office Supplies Ltd", "GB123456782", "41926837"),
                    ("Gulf Coast Pallets Inc", "76-3049512", "000123456789")]
            con.executemany("INSERT INTO vendor_registry(legal_name,tax_id,account_fp,account_last4,source) "
                            "VALUES(?,?,?,?,?)",
                            [(n, t, privacy.fingerprint(a), a[-4:], "ERP seed") for n, t, a in seed])


def masked_payload(p):
    p = json.loads(json.dumps(p))
    if g(p, "bank.account_number"):
        p["bank"]["account_number"] = privacy.mask(p["bank"]["account_number"])
    return p


def save_run(run):
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (run["id"], run["created_at"], run["vendor_name"], run["country"], run["status"],
                     run["current_stage"], json.dumps(masked_payload(run["payload"])), json.dumps(run["events"]),
                     json.dumps(run.get("result")), run.get("resubmission_of")))


def load_run(run_id):
    with db() as con:
        r = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("payload", "events", "result"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def list_runs():
    with db() as con:
        rows = con.execute("SELECT id,created_at,vendor_name,country,status,current_stage,result,resubmission_of "
                           "FROM runs ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        res = (json.loads(d.pop("result")) if d["result"] else None) or {}
        d["summary"] = res.get("summary", "")
        d["counts"] = res.get("counts", {})
        out.append(d)
    return out


# ---------------- Documents ----------------
def read_pdf(path):
    try:
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception:
        return ""


# ---------------- Pipeline ----------------
def chk(sev, rule, detail, ask=None, evidence=None):
    return {"severity": sev, "rule": rule, "detail": detail, "vendor_ask": ask, "evidence": evidence}


def g(d, path, default=""):
    for p in path.split("."):
        d = (d or {}).get(p) if isinstance(d, dict) else None
    return default if d is None else d


def stage_intake(p, docs):
    out = []
    required = {
        "legal_name": "Registered legal name", "country": "Country",
        "address.line1": "Registered address", "address.city": "City", "address.state": "State / region",
        "address.postal_code": "Postal code", "contact.name": "Contact person", "contact.email": "Contact email",
        "contact.phone": "Contact phone", "tax.tax_id": "Tax registration number",
        "bank.account_holder": "Bank account holder name", "bank.account_number": "Bank account number",
        "bank.bank_name": "Bank name",
    }
    country = g(p, "country")
    if country == "IN":
        required.update({"tax.pan": "PAN", "bank.ifsc": "IFSC code"})
    elif country == "US":
        required["bank.routing_number"] = "ABA routing number"
    elif country == "GB":
        required["bank.sort_code"] = "Sort code"
    elif country:
        out.append(chk("fix", "Supported country", f"Country '{country}' is not yet supported (IN, US, GB)",
                       "We currently onboard vendors registered in India, the US or the UK. Please confirm your country of registration."))
    missing = [label for path, label in required.items() if not str(g(p, path)).strip()]
    for label in missing:
        out.append(chk("fix", "Required field", f"Missing: {label}", f"Please provide your {label.lower()}."))
    if not missing:
        out.append(chk("pass", "Required fields", f"All {len(required)} required fields present"))
    for dtype, label in REQUIRED_DOCS.items():
        if dtype not in docs:
            out.append(chk("fix", "Required document", f"Missing document: {label}", f"Please upload your {label.lower()}."))
        else:
            out.append(chk("pass", "Required document", f"{label} received ({os.path.basename(docs[dtype])})"))
    return out


def stage_documents(p, docs, ctx):
    out, sent, local, redactions = [], 0, 0, 0
    legal = g(p, "legal_name")
    for dtype, path in docs.items():
        label = REQUIRED_DOCS.get(dtype, dtype)
        text = read_pdf(path)
        ctx["doc_text"][dtype] = text
        if len(text.strip()) < 20:
            # Scans are never sent to an AI as images: route to a human instead.
            out.append(chk("fix", "Readable document", f"{label}: no extractable text (scanned image or empty file)",
                           f"Your {label.lower()} could not be read. Please upload a clear, text-based PDF."))
            continue
        ex = ai_extract.extract(text, dtype)
        f = ex["fields"]
        if ex["mode"] == "claude":
            sent, redactions = sent + 1, redactions + ex["redactions"]
        else:
            local += 1
        how = {"claude": f"AI-read, {ex['redactions']} identifiers redacted"}.get(ex["mode"], ex["mode"])
        evidence = ex["sent_text"]
        found, conf = f.get("doc_type") or "other", float(f.get("confidence") or 0)
        if found != dtype:
            what = found.replace("_", " ")
            out.append(chk("fix", "Correct document type",
                           f"Uploaded as '{label}' but content looks like: {what} (confidence {conf:.0%}; {how})",
                           f"The file you uploaded as your {label.lower()} appears to be a {what}. Please upload the correct document.",
                           evidence))
            ctx["bad_docs"].add(dtype)
            continue
        if conf < 0.6:
            out.append(chk("warn", "Correct document type",
                           f"{label}: type unclear (confidence {conf:.0%}) - flag for human review ({how})", None, evidence))
        else:
            out.append(chk("pass", "Correct document type", f"{label}: matches (confidence {conf:.0%}; {how})", None, evidence))
        name = f.get("entity_name")
        if dtype != "bank_proof" and name:   # bank holder name is checked in the bank stage
            sim = V.name_similarity(name, legal)
            if sim < 80:
                out.append(chk("fix", "Document issued to this company",
                               f"{label} is issued to '{name}', not '{legal}' ({sim}%)",
                               f"Your {label.lower()} is issued to '{name}'. Please upload the one issued to {legal}."))
            else:
                out.append(chk("pass", "Document issued to this company", f"{label}: issued to '{name}'"))
        exp = f.get("expiry_date")
        if exp and exp < datetime.now(timezone.utc).date().isoformat():
            out.append(chk("fix", "Document in date", f"{label} expired on {exp}",
                           f"Your {label.lower()} expired on {exp}. Please upload a current one."))
        for flag in (f.get("red_flags") or [])[:3]:
            out.append(chk("warn", "AI red flag", f"{label}: {flag}"))
    out.append(chk("pass", "Data handling",
                   f"{local} document(s) processed on this server only; {sent} sent to AI as redacted text "
                   f"({redactions} identifiers removed). Bank numbers never leave the server."))
    # extracted values vs form
    tax_text = ctx["doc_text"].get("tax_certificate", "") if "tax_certificate" not in ctx["bad_docs"] else ""
    form_tax = str(g(p, "tax.tax_id")).replace(" ", "").replace("-", "").upper()
    if tax_text and form_tax:
        compact = re.sub(r"[\s-]", "", tax_text.upper())
        if form_tax in compact:
            out.append(chk("pass", "Tax ID matches certificate", "Tax ID on form appears on the certificate"))
        else:
            out.append(chk("fix", "Tax ID matches certificate", "Tax ID on the form does NOT appear on the uploaded certificate",
                           "The tax number in your form does not match your tax certificate. Please correct whichever is wrong."))
    bank_text = ctx["doc_text"].get("bank_proof", "") if "bank_proof" not in ctx["bad_docs"] else ""
    acct = str(g(p, "bank.account_number")).strip()
    if bank_text and acct:
        if acct in re.sub(r"[\s-]", "", bank_text):
            out.append(chk("pass", "Account matches bank proof", "Account number on form appears on bank proof"))
        else:
            out.append(chk("fix", "Account matches bank proof", "Account number on form does NOT appear on bank proof",
                           "The account number in your form does not match your bank document."))
    return out


def stage_tax(p, ctx):
    out = []
    country, tax_id = g(p, "country"), str(g(p, "tax.tax_id")).strip()
    if not tax_id or country not in V.TAX_VALIDATORS:
        return [chk("warn", "Tax ID", "Skipped — tax ID or supported country missing")]
    looks_like = V.detect_tax_country(tax_id)
    if looks_like and looks_like != country:
        out.append(chk("fix", "Tax ID matches country",
                       f"Vendor claims {country} but tax ID looks like a {V.TAX_LABEL[looks_like]} ({looks_like})",
                       f"Vendors registered in {country} must provide a {V.TAX_LABEL[country]}. The number provided looks like a {looks_like} registration."))
        return out
    ok, msg = V.TAX_VALIDATORS[country](tax_id)
    out.append(chk("pass" if ok else "fix", f"{V.TAX_LABEL[country]} validation", msg,
                   None if ok else f"Your {V.TAX_LABEL[country]} '{tax_id}' is not valid ({msg}). Please re-check it."))
    if country == "IN":
        ok2, msg2 = V.validate_pan(g(p, "tax.pan"))
        out.append(chk("pass" if ok2 else "fix", "PAN validation", msg2, None if ok2 else "Please re-check your PAN."))
        ctx["tax_ok"] = ok and ok2
    else:
        ctx["tax_ok"] = ok
    return out


def stage_bank(p):
    out = []
    country = g(p, "country")
    legal, holder = g(p, "legal_name"), g(p, "bank.account_holder")
    if holder:
        score = V.name_similarity(legal, holder)
        if score >= 90:
            out.append(chk("pass", "Account holder = legal entity", f"'{holder}' matches '{legal}' (similarity {score}%)"))
        elif score >= 70:
            out.append(chk("warn", "Account holder = legal entity",
                           f"'{holder}' is close to '{legal}' ({score}%) — likely abbreviation/trade name"))
        else:
            out.append(chk("fix", "Account holder = legal entity",
                           f"Account holder '{holder}' does not match legal name '{legal}' ({score}%). Payment to a third-party or personal account is a top fraud pattern.",
                           f"Your bank account is held by '{holder}', not '{legal}'. We can only pay accounts held by the registered entity. Please provide a business account in the company's name, or a bank letter confirming '{holder}' is the same legal entity."))
    if country == "IN":
        ifsc = str(g(p, "bank.ifsc")).upper()
        ok = bool(V.IFSC_RE.match(ifsc))
        out.append(chk("pass" if ok else "fix", "IFSC format", f"IFSC {ifsc} {'valid' if ok else 'invalid (4 letters + 0 + 6 chars)'}",
                       None if ok else "Please re-check your IFSC code."))
        acct = str(g(p, "bank.account_number"))
        ok = bool(re.fullmatch(r"\d{9,18}", acct))
        out.append(chk("pass" if ok else "fix", "Account number format", f"{len(acct)} digits {'ok' if ok else '— Indian accounts are 9–18 digits'}",
                       None if ok else "Please re-check your account number."))
    elif country == "US":
        ok, msg = V.validate_aba(g(p, "bank.routing_number"))
        out.append(chk("pass" if ok else "fix", "ABA routing", msg, None if ok else "Please re-check your routing number."))
    elif country == "GB":
        sc = str(g(p, "bank.sort_code")).replace("-", "")
        ok = bool(re.fullmatch(r"\d{6}", sc))
        out.append(chk("pass" if ok else "fix", "Sort code", f"Sort code {'valid' if ok else 'must be 6 digits'}",
                       None if ok else "Please re-check your sort code."))
        acct = str(g(p, "bank.account_number"))
        ok = bool(re.fullmatch(r"\d{8}", acct))
        out.append(chk("pass" if ok else "fix", "Account number format", f"UK account numbers are 8 digits ({'ok' if ok else 'got ' + str(len(acct))})",
                       None if ok else "Please re-check your account number."))
    bank_country = g(p, "bank.country") or country
    if bank_country != country:
        out.append(chk("fix", "Bank country", f"Bank is in {bank_country} but vendor is registered in {country}",
                       "Your bank account is in a different country from your registration. Please provide a domestic account or explain the arrangement."))
    return out


def stage_consistency(p):
    out = []
    country = g(p, "country")
    if country == "IN":
        gst, pan, state = str(g(p, "tax.tax_id")).upper(), str(g(p, "tax.pan")).upper(), g(p, "address.state")
        if len(gst) == 15:
            gst_state = V.GST_STATE_CODES.get(gst[:2])
            if gst_state and state and gst_state.lower() != state.strip().lower():
                out.append(chk("fix", "GSTIN state = address state",
                               f"GSTIN prefix {gst[:2]} = {gst_state}, but address is in {state}",
                               f"Your GSTIN is registered in {gst_state}, but your address is in {state}. Please share the GSTIN for your {state} registration, or correct the address."))
            elif gst_state:
                out.append(chk("pass", "GSTIN state = address state", f"Both {gst_state}"))
            if pan and gst[2:12] != pan:
                out.append(chk("fix", "PAN embedded in GSTIN", f"GSTIN contains PAN {gst[2:12]}, form says {pan}",
                               "The PAN in your form does not match the PAN inside your GSTIN. Please confirm the correct PAN."))
            elif pan:
                out.append(chk("pass", "PAN embedded in GSTIN", "PAN matches characters 3–12 of GSTIN"))
        if len(pan) == 10 and re.search(r"\b(pvt|private|ltd|limited)\b", g(p, "legal_name").lower()) and pan[3] != "C":
            out.append(chk("warn", "PAN entity type", f"Name suggests a company but PAN 4th char is '{pan[3]}' (companies use 'C')"))
    pc = str(g(p, "address.postal_code")).upper().strip()
    if country in V.POSTAL_RE and pc:
        ok = bool(re.match(V.POSTAL_RE[country], pc))
        out.append(chk("pass" if ok else "fix", "Postal code format", f"'{pc}' {'fits' if ok else 'does not fit'} {country} format",
                       None if ok else "Please re-check your postal code."))
    phone = str(g(p, "contact.phone")).replace(" ", "")
    if country in V.PHONE_PREFIX and phone and not phone.startswith(V.PHONE_PREFIX[country]):
        out.append(chk("warn", "Phone country code", f"Phone {phone} doesn't start with {V.PHONE_PREFIX[country]}"))
    email_dom, web_dom = V.domain_of(g(p, "contact.email")), V.domain_of(g(p, "website"))
    if email_dom in V.FREE_EMAIL:
        out.append(chk("warn", "Business email", f"Contact uses free email domain ({email_dom})"))
    elif web_dom and email_dom and email_dom != web_dom:
        out.append(chk("warn", "Email domain = website", f"Email domain {email_dom} ≠ website {web_dom}"))
    elif email_dom:
        out.append(chk("pass", "Email domain = website", f"Both {email_dom}"))
    if not out:
        out.append(chk("pass", "Cross-field", "No inconsistencies found"))
    return out


def stage_risk(p):
    out = []
    legal, acct = g(p, "legal_name"), str(g(p, "bank.account_number")).strip()
    tax = str(g(p, "tax.tax_id")).replace("-", "").upper()
    for w in WATCHLIST:
        s = V.name_similarity(legal, w)
        if s >= 90:
            out.append(chk("block", "Sanctions / watchlist", f"'{legal}' matches watchlist entry '{w}' ({s}%)"))
    with db() as con:
        master = con.execute("SELECT legal_name,tax_id,account_fp,account_last4,source FROM vendor_registry").fetchall()
    fp = privacy.fingerprint(acct)
    for m in master:
        same_entity = m["tax_id"].replace("-", "").upper() == tax or V.name_similarity(m["legal_name"], legal) >= 95
        if fp and m["account_fp"] == fp and not same_entity:
            out.append(chk("block", "Bank account reuse",
                           f"Account ••••{m['account_last4']} already belongs to existing vendor '{m['legal_name']}' ({m['source']}). "
                           "Matched by fingerprint - no account numbers are stored. Shared accounts across unrelated vendors = payment-diversion fraud."))
        if tax and m["tax_id"].replace("-", "").upper() == tax and V.name_similarity(m["legal_name"], legal) < 95:
            out.append(chk("block", "Tax ID reuse", f"Tax ID already registered to '{m['legal_name']}'"))
        elif tax and m["tax_id"].replace("-", "").upper() == tax:
            out.append(chk("warn", "Already onboarded", f"Vendor already exists in registry ({m['source']}) - possible duplicate"))
    if not out:
        out.append(chk("pass", "Risk screening", f"No watchlist hits; bank account and tax ID unique across {len(master)} vendors"))
    return out


def draft_message(p, status, asks):
    name = g(p, "contact.name") or "there"
    company = g(p, "legal_name") or "your company"
    if status == "approved":
        return f"Hi {name},\n\nGood news — {company} has been approved as a vendor. You'll receive payment set-up confirmation shortly.\n\nThanks,\nVendor Onboarding Team"
    if status == "rejected":
        # Deliberately vague: never tell a suspected fraudster which check fired.
        return (f"Hi {name},\n\nThank you for submitting your onboarding details for {company}. "
                "We are unable to proceed with this application at this time. "
                "If you believe this is an error, please contact your procurement contact directly.\n\nVendor Onboarding Team")
    asks = list(dict.fromkeys(asks))
    items = "\n".join(f"  {i}. {a}" for i, a in enumerate(asks, 1))
    opener = ("We're almost there — we need the following before we can approve you:" if len(asks) <= 4
              else f"Your submission is missing or has issues with {len(asks)} items. Please address the following:")
    closer = "Everything else checked out." if len(asks) <= 4 else "Once these are in, we'll re-run verification straight away."
    base = (f"Hi {name},\n\nThanks for submitting {company}'s onboarding details. {opener}\n\n{items}\n\n"
            f"Reply to this email or resubmit the form with the corrections. {closer}\n\n"
            "Thanks,\nVendor Onboarding Team")
    return llm_polish(base) or base


def llm_polish(text):
    """Optional: if ANTHROPIC_API_KEY is set, let Claude tidy the wording. Facts stay fixed."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        body = json.dumps({"model": os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 600,
                           "messages": [{"role": "user", "content":
                               "Rewrite this vendor email to be warm, clear and concise. Keep every numbered request "
                               "and every fact exactly; do not add new requests. Return only the email.\n\n" + text}]})
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body.encode(),
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())["content"][0]["text"].strip()
    except Exception:
        return None


def new_run(payload, docs, resubmission_of=None):
    run = {"id": uuid.uuid4().hex[:10], "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "vendor_name": payload.get("legal_name") or "(no name)", "country": payload.get("country", ""),
           "status": "running", "current_stage": "intake", "payload": payload,
           "events": [{"key": k, "label": l, "state": "queued", "checks": []} for k, l in STAGES],
           "result": None, "resubmission_of": resubmission_of, "_docs": docs}
    save_run(run)
    with db() as con:
        con.execute("INSERT OR REPLACE INTO run_secrets VALUES(?,?)", (run["id"], g(payload, "bank.account_number")))
    return run


def secret_account(run_id):
    with db() as con:
        r = con.execute("SELECT account_number FROM run_secrets WHERE run_id=?", (run_id,)).fetchone()
    return r[0] if r else ""


def execute(run):
    p, docs = run["payload"], run.pop("_docs")
    ctx = {"doc_text": {}, "bad_docs": set(), "tax_ok": False}
    runners = {
        "intake": lambda: stage_intake(p, docs), "documents": lambda: stage_documents(p, docs, ctx),
        "tax": lambda: stage_tax(p, ctx), "bank": lambda: stage_bank(p),
        "consistency": lambda: stage_consistency(p), "risk": lambda: stage_risk(p),
    }
    all_checks = []
    for ev in run["events"]:
        run["current_stage"] = ev["key"]
        ev["state"], ev["started"] = "running", time.time()
        save_run(run)
        time.sleep(STAGE_DELAY)
        if ev["key"] == "decision":
            break
        try:
            ev["checks"] = runners[ev["key"]]()
        except Exception as e:  # a crashing rule must never silently approve
            ev["checks"] = [chk("fix", "Internal error", f"Stage failed: {e}", None)]
        all_checks += ev["checks"]
        sevs = {c["severity"] for c in ev["checks"]}
        ev["state"] = "block" if "block" in sevs else "fix" if "fix" in sevs else "warn" if "warn" in sevs else "pass"
        ev["ms"] = int((time.time() - ev["started"]) * 1000)
        save_run(run)

    counts = {s: sum(c["severity"] == s for c in all_checks) for s in ("pass", "warn", "fix", "block")}
    status = "rejected" if counts["block"] else "pending" if counts["fix"] else "approved"
    asks = [c["vendor_ask"] for c in all_checks if c["severity"] == "fix" and c["vendor_ask"]]
    reasons = [c["detail"] for c in all_checks if c["severity"] in ("block", "fix")]
    summary = {"approved": f"All {counts['pass']} checks passed" + (f", {counts['warn']} warnings noted" if counts["warn"] else ""),
               "pending": f"{counts['fix']} item(s) need vendor action",
               "rejected": f"{counts['block']} blocking risk signal(s)"}[status]
    dec = run["events"][-1]
    dec["checks"] = [chk({"approved": "pass", "pending": "fix", "rejected": "block"}[status], "Decision",
                         f"{status.upper()} — {summary}")]
    dec["state"] = dec["checks"][0]["severity"]
    run["status"], run["current_stage"] = status, "done"
    run["result"] = {"status": status, "summary": summary, "counts": counts, "reasons": reasons,
                     "vendor_message": draft_message(p, status, asks),
                     "internal_note": "; ".join(reasons) if reasons else "Clean submission"}
    if status == "approved":
        with db() as con:
            acct = g(p, "bank.account_number")
            con.execute("INSERT INTO vendor_registry(legal_name,tax_id,account_fp,account_last4,source) VALUES(?,?,?,?,?)",
                        (p["legal_name"], g(p, "tax.tax_id"), privacy.fingerprint(acct), str(acct)[-4:], f"run {run['id']}"))
    save_run(run)
    # Uploaded files contain bank data: delete them once the run is done (samples are kept).
    upload_root = os.path.abspath(UPLOAD_DIR)
    for path in docs.values():
        if os.path.abspath(path).startswith(upload_root):
            try:
                os.remove(path)
            except OSError:
                pass
    return run
