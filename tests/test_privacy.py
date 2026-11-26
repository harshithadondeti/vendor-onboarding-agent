import ai_extract, privacy

BANK_LETTER = """HDFC Bank - Bank Confirmation Letter
Account Holder: Bharat Freight Solutions Pvt Ltd
Account Number: 50200012345678
IFSC: HDFC0000123
Contact: anita.desai@bharatfreight.in, +91 22 4000 1234"""

GST_CERT = """Form GST REG-06 Registration Certificate - Goods and Services Tax
GSTIN: 27AAACB7712M1ZX  PAN AAACB7712M
Legal Name: Bharat Freight Solutions Private Limited
Date of Liability: 01/04/2019  Pin 400001"""


def test_redaction_removes_every_identifier():
    red, vault = privacy.redact(BANK_LETTER + "\n" + GST_CERT)
    for secret in ["50200012345678", "HDFC0000123", "27AAACB7712M1ZX", "AAACB7712M",
                   "anita.desai@bharatfreight.in", "4000 1234"]:
        assert secret not in red, secret
    assert "Bharat Freight Solutions" in red        # company names stay - the AI needs them
    assert "01/04/2019" in red and "400001" in red  # dates and pin codes are not identifiers


def test_same_value_gets_same_token_and_round_trips():
    red, vault = privacy.redact("acct 50200012345678 again 50200012345678")
    assert red.count("[NUM_1]") == 2
    assert privacy.detokenize({"a": "[NUM_1]"}, vault) == {"a": "50200012345678"}


def test_bank_documents_never_reach_the_llm():
    calls = []
    res = ai_extract.extract(BANK_LETTER, "bank_proof", llm=lambda prompt: calls.append(prompt))
    assert calls == [] and res["mode"] == "local-only (policy)"


def test_llm_only_sees_redacted_text_and_answers_are_detokenized():
    seen = {}

    def fake_llm(prompt):
        seen["prompt"] = prompt
        return {"doc_type": "tax_certificate", "confidence": 0.95, "tax_id": "[TAX_ID_1]",
                "entity_name": "Bharat Freight Solutions Private Limited", "red_flags": []}

    res = ai_extract.extract(GST_CERT, "tax_certificate", llm=fake_llm)
    assert "27AAACB7712M1ZX" not in seen["prompt"] and "AAACB7712M" not in seen["prompt"]
    assert res["fields"]["tax_id"] == "27AAACB7712M1ZX"
    assert res["mode"] == "claude"


def test_llm_failure_falls_back_to_local():
    def broken(prompt):
        raise TimeoutError("api down")
    res = ai_extract.extract(GST_CERT, "tax_certificate", llm=broken)
    assert res["mode"].startswith("local fallback") and res["fields"]["doc_type"] == "tax_certificate"


def test_mask_and_fingerprint():
    assert privacy.mask("50200012345678") == "••••5678"
    assert privacy.fingerprint("5020 0012 345678") == privacy.fingerprint("50200012345678")
    assert "50200012345678" not in privacy.fingerprint("50200012345678")
