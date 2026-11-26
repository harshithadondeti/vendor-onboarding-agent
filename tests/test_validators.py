import pytest
import validators as V


def test_gstin_valid_checksum():
    base = "27AAACB7712M1Z"
    assert V.validate_gstin(base + V.gstin_check_char(base))[0]


def test_gstin_bad_checksum():
    base = "27AAACB7712M1Z"
    wrong = "0" if V.gstin_check_char(base) != "0" else "1"
    ok, msg = V.validate_gstin(base + wrong)
    assert not ok and "checksum" in msg


@pytest.mark.parametrize("ein,ok", [("74-2841937", True), ("00-1234567", False), ("7412", False)])
def test_ein(ein, ok):
    assert V.validate_ein(ein)[0] is ok


@pytest.mark.parametrize("aba,ok", [("111000025", True), ("111000026", False)])
def test_aba(aba, ok):
    assert V.validate_aba(aba)[0] is ok


def test_uk_vat_checksum_fails():
    assert not V.validate_uk_vat("GB987654321")[0]


def test_iban():
    assert V.validate_iban("GB82 WEST 1234 5698 7654 32")[0]
    assert not V.validate_iban("GB83 WEST 1234 5698 7654 32")[0]


def test_tax_id_from_wrong_country_is_detected():
    assert V.detect_tax_country("74-2841937") == "US"


def test_name_matching_ignores_legal_suffixes():
    assert V.name_similarity("Kaveri Textiles Pvt. Ltd.", "Kaveri Textiles Private Limited") >= 90
    assert V.name_similarity("Rajesh Patel", "Thames Valley Components Ltd") < 70
