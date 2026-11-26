"""Deterministic validators: tax IDs, bank identifiers, name matching.

Every function returns plain values so the rules engine can explain itself.
"""
import re
from rapidfuzz import fuzz

B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# ---------- India ----------
GST_STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "32": "Kerala", "33": "Tamil Nadu", "36": "Telangana",
    "37": "Andhra Pradesh",
}
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def gstin_check_char(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        p = B36.index(ch) * (1 if i % 2 == 0 else 2)
        total += p // 36 + p % 36
    return B36[(36 - total % 36) % 36]


def validate_gstin(g: str):
    g = (g or "").strip().upper()
    if not GSTIN_RE.match(g):
        return False, "GSTIN must be 15 characters: 2-digit state code + 10-char PAN + entity no. + 'Z' + check char"
    if g[:2] not in GST_STATE_CODES:
        return False, f"GSTIN state code {g[:2]} is not a valid Indian state code"
    if gstin_check_char(g[:14]) != g[14]:
        return False, "GSTIN checksum digit is wrong (likely a typo or fabricated number)"
    return True, "GSTIN format and checksum valid"


def validate_pan(p: str):
    p = (p or "").strip().upper()
    if not PAN_RE.match(p):
        return False, "PAN must be 5 letters + 4 digits + 1 letter"
    return True, "PAN format valid"


# ---------- United States ----------
EIN_RE = re.compile(r"^\d{2}-?\d{7}$")
INVALID_EIN_PREFIXES = {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70",
                        "78", "79", "89", "96", "97"}


def validate_ein(e: str):
    e = (e or "").strip()
    if not EIN_RE.match(e):
        return False, "US EIN must be 9 digits in the form XX-XXXXXXX"
    if e[:2] in INVALID_EIN_PREFIXES:
        return False, f"EIN prefix {e[:2]} is never issued by the IRS"
    return True, "EIN format and prefix valid"


def validate_aba(r: str):
    r = (r or "").strip()
    if not re.fullmatch(r"\d{9}", r):
        return False, "ABA routing number must be 9 digits"
    d = [int(c) for c in r]
    s = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
    if s % 10:
        return False, "ABA routing checksum fails"
    return True, "ABA routing number checksum valid"


# ---------- United Kingdom ----------
def uk_vat_valid_digits(nine: str) -> bool:
    d = [int(c) for c in nine]
    total = sum(d[i] * w for i, w in enumerate(range(8, 1, -1))) + int(nine[7:9])
    return total % 97 == 0 or (total + 55) % 97 == 0


def validate_uk_vat(v: str):
    v = (v or "").replace(" ", "").upper()
    if not re.fullmatch(r"GB\d{9}", v):
        return False, "UK VAT number must be 'GB' followed by 9 digits"
    if not uk_vat_valid_digits(v[2:]):
        return False, "UK VAT modulus-97 check fails"
    return True, "UK VAT format and checksum valid"


def validate_iban(iban: str):
    s = (iban or "").replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False, "IBAN format invalid"
    rearr = s[4:] + s[:4]
    num = "".join(str(B36.index(c)) for c in rearr)
    if int(num) % 97 != 1:
        return False, "IBAN checksum fails"
    return True, "IBAN checksum valid"


TAX_VALIDATORS = {"IN": validate_gstin, "US": validate_ein, "GB": validate_uk_vat}
TAX_LABEL = {"IN": "GSTIN", "US": "EIN", "GB": "VAT number"}
POSTAL_RE = {"IN": r"^\d{6}$", "US": r"^\d{5}(-\d{4})?$", "GB": r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"}
PHONE_PREFIX = {"IN": "+91", "US": "+1", "GB": "+44"}


def detect_tax_country(tax_id: str):
    """Which country's format does this tax ID actually look like?"""
    t = (tax_id or "").replace(" ", "").upper()
    if GSTIN_RE.match(t):
        return "IN"
    if EIN_RE.match(t):
        return "US"
    if re.fullmatch(r"GB\d{9}", t):
        return "GB"
    return None


# ---------- Names ----------
SUFFIXES = r"\b(private|pvt|limited|ltd|llc|inc|incorporated|corp|corporation|co|company|plc|llp|the)\b"


def normalize_name(n: str) -> str:
    n = (n or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(SUFFIXES, " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_similarity(a: str, b: str) -> int:
    return int(fuzz.token_sort_ratio(normalize_name(a), normalize_name(b)))


FREE_EMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "rediffmail.com", "proton.me", "icloud.com"}


def domain_of(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.split("@")[-1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s
