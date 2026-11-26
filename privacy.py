"""Privacy layer: everything sensitive is handled here, before any text can reach an LLM.

Three tools:
  redact()     - swaps identifiers (account numbers, IBAN, tax IDs, PAN, IFSC, phones, emails)
                 for tokens like [NUM_1]; the real values stay in a local 'vault' dict.
  detokenize() - puts real values back into the LLM's answer, locally.
  mask()/fingerprint() - for display (••••1234) and for duplicate checks (keyed hash, no plaintext).
"""
import hashlib, hmac, os, re

HASH_KEY = os.environ.get("ACCOUNT_HASH_KEY", "dev-only-change-me").encode()

# Order matters: most specific patterns first.
PATTERNS = [
    ("EMAIL", r"[\w.+-]+@[\w-]+\.[\w.]+"),
    ("IBAN", r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
    ("TAX_ID", r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"),   # GSTIN
    ("PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b"),
    ("TAX_ID", r"\bGB ?\d{9}\b"),                                    # UK VAT
    ("TAX_ID", r"\b\d{2}-\d{7}\b"),                                  # US EIN
    ("BANK_CODE", r"\b[A-Z]{4}0[A-Z0-9]{6}\b"),                      # IFSC
    ("BANK_CODE", r"\b\d{2}-\d{2}-\d{2}\b"),                         # UK sort code
    ("PHONE", r"\+\d{1,3}[\d ]{7,14}\d"),
    ("NUM", r"\b\d[\d -]{6,22}\d\b"),                                # account / routing numbers
]


def redact(text: str):
    vault, seen, counts = {}, {}, {}

    def make_sub(label):
        def sub(m):
            raw = m.group(0)
            if label == "NUM" and not 8 <= sum(c.isdigit() for c in raw) <= 18:
                return raw  # short numbers (dates, pin codes, amounts) are not identifiers
            if raw in seen:
                return seen[raw]
            counts[label] = counts.get(label, 0) + 1
            token = f"[{label}_{counts[label]}]"
            seen[raw], vault[token] = token, raw
            return token
        return sub

    out = text or ""
    for label, pat in PATTERNS:
        out = re.sub(pat, make_sub(label), out)
    return out, vault


def detokenize(obj, vault):
    if isinstance(obj, str):
        for tok, raw in vault.items():
            obj = obj.replace(tok, raw)
        return obj
    if isinstance(obj, list):
        return [detokenize(x, vault) for x in obj]
    if isinstance(obj, dict):
        return {k: detokenize(v, vault) for k, v in obj.items()}
    return obj


def mask(value) -> str:
    s = re.sub(r"\s", "", str(value or ""))
    return "••••" + s[-4:] if len(s) > 4 else s


def fingerprint(value) -> str:
    """Keyed hash so we can detect the same account across vendors without storing it."""
    s = re.sub(r"[\s-]", "", str(value or ""))
    return hmac.new(HASH_KEY, s.encode(), hashlib.sha256).hexdigest() if s else ""


def is_masked(value) -> bool:
    return str(value or "").startswith("••••")
