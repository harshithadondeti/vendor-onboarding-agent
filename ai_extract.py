"""Document understanding.

Policy:
  * Bank documents (LOCAL_ONLY_DOCS) never leave the server - parsed with local rules only.
  * Other documents: text is extracted locally, identifiers are redacted, and only the
    redacted text is sent to Claude. Images/scans are never sent.
  * The AI extracts and flags. It never approves or rejects - the rules engine does.
  * No ANTHROPIC_API_KEY (or API error) -> local fallback, so the app always runs.
"""
import json, os, re, urllib.request
import privacy

LOCAL_ONLY_DOCS = set(filter(None, os.environ.get("LOCAL_ONLY_DOCS", "bank_proof").split(",")))
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5-5")

DOC_SIGNALS = {
    "tax_certificate": ["goods and services tax", "gstin", "reg-06", "employer identification number",
                        "internal revenue service", "cp 575", "vat registration", "hm revenue"],
    "bank_proof": ["account number", "ifsc", "routing number", "sort code", "account holder",
                   "cancelled cheque", "bank confirmation", "bank statement"],
    "incorporation_certificate": ["certificate of incorporation", "registrar of companies", "companies act",
                                  "secretary of state", "companies house", "articles of organization"],
    "invoice": ["invoice", "bill to", "amount due", "subtotal", "due date"],
}

PROMPT = """You review documents submitted by a vendor during onboarding.
Sensitive identifiers were replaced with tokens such as [TAX_ID_1] or [NUM_2]. Never invent real values:
if a field's value is a token, return the token exactly as written.

The vendor uploaded this file as: {declared}

Return ONLY a JSON object, no prose:
{{"doc_type": "tax_certificate" | "bank_proof" | "incorporation_certificate" | "invoice" | "other",
  "confidence": number 0-1 (how sure you are about doc_type),
  "entity_name": "the company the document is issued to, or null",
  "tax_id": "token or null",
  "issue_date": "YYYY-MM-DD or null",
  "expiry_date": "YYYY-MM-DD or null",
  "red_flags": ["short notes on anything suspicious, e.g. a different company named, inconsistent dates"]}}

Document text:
---
{text}
---"""


def _date(s):
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def local_extract(text, declared):
    t = text.lower()
    scores = {k: sum(s in t for s in sig) for k, sig in DOC_SIGNALS.items()}
    best = max(scores, key=scores.get)
    doc_type = best if scores[best] else "other"
    name = None
    for pat in [r"(?:Legal Name|Name|Account Holder)\s*:\s*(.+)", r"certify that\s+(.+)"]:
        m = re.search(pat, text, re.I)
        if m:
            name = m.group(1).strip()
            break
    exp = re.search(r"(?:valid until|expiry date|expires on)\s*:?\s*(\d{2}/\d{2}/\d{4})", text, re.I)
    return {"doc_type": doc_type, "confidence": round(min(1.0, scores.get(doc_type, 0) / 3), 2),
            "entity_name": name, "issue_date": None, "expiry_date": _date(exp.group(1)) if exp else None,
            "red_flags": []}


def _call_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({"model": MODEL, "max_tokens": 500, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = json.loads(r.read())["content"][0]["text"]
    return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))


def extract(text, declared, llm=None):
    """Returns {fields, mode, sent_text, redactions}. `llm` is injectable for tests."""
    if declared in LOCAL_ONLY_DOCS:
        return {"fields": local_extract(text, declared), "mode": "local-only (policy)",
                "sent_text": None, "redactions": 0}
    redacted, vault = privacy.redact(text)
    try:
        ans = (llm or _call_claude)(PROMPT.format(declared=declared, text=redacted[:12000]))
    except Exception as e:  # network/API/JSON failure -> never block the run
        ans, err = None, str(e)[:80]
    else:
        err = None
    if ans is None:
        return {"fields": local_extract(text, declared), "mode": "local fallback" + (f" ({err})" if err else ""),
                "sent_text": None, "redactions": len(vault)}
    fields = privacy.detokenize(ans, vault)
    fields["confidence"] = float(fields.get("confidence") or 0)
    return {"fields": fields, "mode": "claude", "sent_text": redacted, "redactions": len(vault)}
