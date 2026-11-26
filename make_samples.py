"""Generates test vendor submissions (JSON + PDF documents) into ./samples."""
import json, os, shutil
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from validators import gstin_check_char

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")


def gstin(state, pan):
    base = f"{state}{pan}1Z"
    return base + gstin_check_char(base)


def pdf(path, title, lines):
    c = canvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica-Bold", 16); c.drawString(60, 790, title)
    c.setFont("Helvetica", 11)
    y = 755
    for ln in lines:
        c.drawString(60, y, ln); y -= 20
    c.save()


def gst_cert(p):
    return ("Government of India - Form GST REG-06", [
        "Registration Certificate - Goods and Services Tax",
        f"GSTIN: {p['tax']['tax_id']}", f"Legal Name: {p['legal_name']}",
        f"Principal Place of Business: {p['address']['line1']}, {p['address']['city']}, {p['address']['state']}",
        "Constitution of Business: Private Limited Company", "Date of Liability: 01/04/2019"])


def ein_letter(p):
    return ("Internal Revenue Service - CP 575", [
        "Department of the Treasury", "We assigned you an Employer Identification Number (EIN)",
        f"EIN: {p['tax']['tax_id']}", f"Name: {p['legal_name']}", f"{p['address']['line1']}, {p['address']['city']}, {p['address']['state']}"])


def vat_cert(p):
    return ("HM Revenue & Customs", ["Certificate of VAT Registration", f"VAT registration number: {p['tax']['tax_id']}",
                                     f"Name: {p['legal_name']}", f"{p['address']['line1']}, {p['address']['city']}"])


def bank_letter(p):
    b = p["bank"]
    code = b.get("ifsc") and f"IFSC: {b['ifsc']}" or b.get("routing_number") and f"Routing Number: {b['routing_number']}" \
        or f"Sort Code: {b.get('sort_code')}"
    return (f"{b['bank_name']} - Bank Confirmation Letter", [
        "To whom it may concern,", f"Account Holder: {b['account_holder']}",
        f"Account Number: {b['account_number']}", code, "This account is active and in good standing."])


def incorp(p):
    reg = {"IN": "Registrar of Companies - Companies Act, 2013", "US": "Secretary of State - Articles of Organization",
           "GB": "Companies House - Companies Act 2006"}[p["country"]]
    return ("Certificate of Incorporation", [reg, f"This is to certify that {p['legal_name']}",
                                             "is incorporated under the law and is a limited company.", "Date of incorporation: 12/06/2016"])


def invoice(p):
    return ("TAX INVOICE", ["Invoice No: SAE/2026/0912", f"From: {p['legal_name']}", "Bill To: Nexa Retail Pvt Ltd",
                            "Item: 400 kg dehydrated onion flakes", "Subtotal: INR 1,84,000", "Amount Due: INR 2,17,120",
                            "Due Date: 15/12/2026"])


TAX_DOC = {"IN": gst_cert, "US": ein_letter, "GB": vat_cert}


def write_case(name, meta, payload, docs):
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "payload.json"), "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(d, "case.json"), "w") as f:
        json.dump(meta, f, indent=2)
    for dtype, (fname, maker) in docs.items():
        title, lines = maker(payload)
        pdf(os.path.join(d, f"{dtype}__{fname}"), title, lines)


def std_docs(p, tax_name="tax_certificate.pdf"):
    return {"tax_certificate": (tax_name, TAX_DOC[p["country"]]),
            "bank_proof": ("bank_letter.pdf", bank_letter),
            "incorporation_certificate": ("incorporation.pdf", incorp)}


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)

    # 1. Happy path - India
    pan = "AAACB7712M"
    p = {"legal_name": "Bharat Freight Solutions Private Limited", "country": "IN", "website": "https://bharatfreight.in",
         "address": {"line1": "14 Ballard Estate, Shoorji Vallabhdas Marg", "city": "Mumbai", "state": "Maharashtra", "postal_code": "400001"},
         "contact": {"name": "Anita Desai", "email": "anita.desai@bharatfreight.in", "phone": "+91 22 4000 1234"},
         "tax": {"tax_id": gstin("27", pan), "pan": pan},
         "bank": {"account_holder": "Bharat Freight Solutions Pvt Ltd", "account_number": "50200012345678",
                  "ifsc": "HDFC0000123", "bank_name": "HDFC Bank", "country": "IN"}}
    write_case("1_happy_india", {"title": "Happy path — Indian freight company", "expect": "approved",
                                 "why": "Every field, document and cross-check lines up."}, p, std_docs(p))

    # 2. Happy path - US
    p = {"legal_name": "Lone Star Packaging LLC", "country": "US", "website": "lonestarpack.com",
         "address": {"line1": "2200 Commerce St", "city": "Dallas", "state": "Texas", "postal_code": "75201"},
         "contact": {"name": "Mark Reyes", "email": "ap@lonestarpack.com", "phone": "+1 214 555 0142"},
         "tax": {"tax_id": "74-2841937"},
         "bank": {"account_holder": "Lone Star Packaging, LLC", "account_number": "4410998877",
                  "routing_number": "111000025", "bank_name": "Frost Bank", "country": "US"}}
    write_case("2_happy_us", {"title": "Happy path — US packaging supplier", "expect": "approved",
                              "why": "Shows the same engine handling a different country's rules (EIN, ABA checksum)."}, p, std_docs(p))

    # 3. Incomplete + wrong document
    pan = "AAICS3390P"
    p = {"legal_name": "Sahyadri Agro Exports Pvt Ltd", "country": "IN", "website": "sahyadriagro.com",
         "address": {"line1": "Plot 22, Hadapsar MIDC", "city": "Pune", "state": "Maharashtra", "postal_code": "411013"},
         "contact": {"name": "Vikram Jadhav", "email": "vikram@sahyadriagro.com", "phone": ""},
         "tax": {"tax_id": gstin("27", pan), "pan": pan},
         "bank": {"account_holder": "Sahyadri Agro Exports Pvt Ltd", "account_number": "001234567890",
                  "ifsc": "ICIC0000456", "bank_name": "ICICI Bank", "country": "IN"}}
    write_case("3_incomplete_wrong_doc", {"title": "Edge — incomplete form + wrong document attached", "expect": "pending",
                                          "why": "No phone, no bank proof, and the 'GST certificate' upload is actually an invoice."},
               p, {"tax_certificate": ("gst_certificate.pdf", invoice), "incorporation_certificate": ("incorporation.pdf", incorp)})

    # 4. UK: personal bank account + bad VAT checksum
    p = {"legal_name": "Thames Valley Components Ltd", "country": "GB", "website": "tvcomponents.co.uk",
         "address": {"line1": "Unit 4, Kennet Side", "city": "Reading", "state": "Berkshire", "postal_code": "RG1 3EA"},
         "contact": {"name": "Rajesh Patel", "email": "rajesh.patel@gmail.com", "phone": "+44 118 496 0000"},
         "tax": {"tax_id": "GB987654321"},
         "bank": {"account_holder": "Rajesh Patel", "account_number": "63849201", "sort_code": "20-45-77",
                  "bank_name": "Barclays", "country": "GB"}}
    write_case("4_uk_name_mismatch", {"title": "Edge — bank account in a person's name + invalid VAT", "expect": "pending",
                                      "why": "Company pays into the director's personal account; VAT fails mod-97; contact uses Gmail."},
               p, std_docs(p))

    # 5. India: subtle cross-field inconsistencies
    real_pan = "AAACK5521Q"
    p = {"legal_name": "Kaveri Textiles Private Limited", "country": "IN", "website": "kaveritextiles.in",
         "address": {"line1": "88 Kalbadevi Road", "city": "Mumbai", "state": "Maharashtra", "postal_code": "400002"},
         "contact": {"name": "Suresh Gowda", "email": "accounts@kaveritextiles.in", "phone": "+91 22 2201 7788"},
         "tax": {"tax_id": gstin("29", real_pan), "pan": "AAACK5521R"},
         "bank": {"account_holder": "Kaveri Textiles Pvt. Ltd.", "account_number": "917020045566123",
                  "ifsc": "UTIB0000004", "bank_name": "Axis Bank", "country": "IN"}}
    write_case("5_cross_field_mismatch", {"title": "Edge — subtle cross-field inconsistencies", "expect": "pending",
                                          "why": "Every field is individually valid, but GSTIN is Karnataka while address is Maharashtra, and the PAN differs by one character from the one inside the GSTIN."},
               p, std_docs(p))

    # 6. Fraud: bank account belongs to an existing vendor
    pan = "AAFCN6610D"
    p = {"legal_name": "Nizam Logistics Private Limited", "country": "IN", "website": "nizamlogistics.in",
         "address": {"line1": "3-6-145 Himayatnagar", "city": "Hyderabad", "state": "Telangana", "postal_code": "500029"},
         "contact": {"name": "Farhan Ali", "email": "farhan@nizamlogistics.in", "phone": "+91 40 2322 9090"},
         "tax": {"tax_id": gstin("36", pan), "pan": pan},
         "bank": {"account_holder": "Nizam Logistics Pvt Ltd", "account_number": "50100277881234",
                  "ifsc": "HDFC0001177", "bank_name": "HDFC Bank", "country": "IN"}}
    write_case("6_fraud_bank_reuse", {"title": "Edge — clean-looking vendor reusing another vendor's bank account", "expect": "rejected",
                                      "why": "Passes every document and format check. Only the cross-vendor check catches that the account already belongs to Deccan Cargo Movers."},
               p, std_docs(p))
    # 7. Certificate issued to a different company (parent / group entity)
    pan = "AAGCT4471H"
    p = {"legal_name": "Trident Cold Chain Private Limited", "country": "IN", "website": "tridentcoldchain.in",
         "address": {"line1": "Survey 41, Shamshabad Road", "city": "Hyderabad", "state": "Telangana", "postal_code": "501218"},
         "contact": {"name": "Priya Reddy", "email": "priya@tridentcoldchain.in", "phone": "+91 40 6600 1200"},
         "tax": {"tax_id": gstin("36", pan), "pan": pan},
         "bank": {"account_holder": "Trident Cold Chain Pvt Ltd", "account_number": "35010045678901",
                  "ifsc": "SBIN0004520", "bank_name": "State Bank of India", "country": "IN"}}
    other = dict(p, legal_name="Trident Group Holdings Limited")
    write_case("7_wrong_entity_document", {"title": "Edge — certificate issued to a different company", "expect": "pending",
                                            "why": "Form and bank are fine, but the incorporation certificate belongs to the parent company. Only reading the document reveals it."},
               p, {"tax_certificate": ("gst_certificate.pdf", gst_cert), "bank_proof": ("bank_letter.pdf", bank_letter),
                   "incorporation_certificate": ("incorporation.pdf", lambda _p: incorp(other))})
    print("samples written to", OUT)


if __name__ == "__main__":
    main()
