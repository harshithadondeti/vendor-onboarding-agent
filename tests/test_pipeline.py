"""End-to-end: every sample case must land on its expected status, with no raw account number stored."""
import json, os, sqlite3
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
CASES = sorted(d for d in os.listdir(SAMPLES) if os.path.isdir(os.path.join(SAMPLES, d)))


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    os.environ["DB_PATH"] = str(tmp_path_factory.mktemp("db") / "t.db")
    os.environ["STAGE_DELAY"] = "0"
    os.environ.pop("ANTHROPIC_API_KEY", None)   # tests run offline and deterministic
    import importlib, engine as e
    importlib.reload(e)
    e.init_db()
    return e


def run_case(engine, name):
    d = os.path.join(SAMPLES, name)
    payload = json.load(open(os.path.join(d, "payload.json")))
    docs = {fn.split("__")[0]: os.path.join(d, fn) for fn in os.listdir(d) if fn.endswith(".pdf")}
    return engine.execute(engine.new_run(payload, docs)), payload


@pytest.mark.parametrize("name", CASES)
def test_case_reaches_expected_status(engine, name):
    expect = json.load(open(os.path.join(SAMPLES, name, "case.json")))["expect"]
    run, _ = run_case(engine, name)
    assert run["status"] == expect, run["result"]["reasons"]
    if expect != "approved":
        assert run["result"]["vendor_message"]


def test_no_raw_account_numbers_in_runs_or_registry(engine):
    run, payload = run_case(engine, "1_happy_india")
    acct = payload["bank"]["account_number"]
    con = sqlite3.connect(os.environ["DB_PATH"])
    dump = "\n".join(str(r) for t in ("runs", "vendor_registry")
                     for r in con.execute(f"SELECT * FROM {t}").fetchall())
    assert acct not in dump
    assert engine.load_run(run["id"])["payload"]["bank"]["account_number"] == "••••" + acct[-4:]


def test_rejection_message_does_not_reveal_the_fraud_signal(engine):
    run, _ = run_case(engine, "6_fraud_bank_reuse")
    msg = run["result"]["vendor_message"].lower()
    assert "account" not in msg and "deccan" not in msg
