import json, os, shutil, threading
from fastapi import FastAPI, UploadFile, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import engine, privacy

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(BASE, "samples")
app = FastAPI(title="Vendor Onboarding Agent")
engine.init_db()


def start(payload, docs, resub=None):
    run = engine.new_run(payload, docs, resub)
    threading.Thread(target=engine.execute, args=(run,), daemon=True).start()
    return {"id": run["id"]}


@app.post("/api/submissions")
async def submit(request: Request):
    """multipart/form-data: 'payload' (JSON string) + files named tax_certificate / bank_proof / incorporation_certificate"""
    form = await request.form()
    try:
        payload = json.loads(form.get("payload") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "payload must be valid JSON")
    tmp = os.path.join(engine.UPLOAD_DIR, "incoming", os.urandom(4).hex())
    os.makedirs(tmp, exist_ok=True)
    docs = {}
    for key in engine.REQUIRED_DOCS:
        f = form.get(key)
        if f is not None and hasattr(f, "filename") and f.filename:
            path = os.path.join(tmp, f"{key}__{os.path.basename(f.filename)}")
            with open(path, "wb") as out:
                shutil.copyfileobj(f.file, out)
            docs[key] = path
    resub = form.get("resubmission_of") or None
    # The UI only ever sees a masked account number. If the vendor left it unchanged,
    # take the real one from the server-side secrets table.
    if resub and privacy.is_masked((payload.get("bank") or {}).get("account_number")):
        payload["bank"]["account_number"] = engine.secret_account(resub)
    return start(payload, docs, resub)


@app.get("/api/samples")
def samples():
    out = []
    for name in sorted(os.listdir(SAMPLES)):
        meta = os.path.join(SAMPLES, name, "case.json")
        if os.path.exists(meta):
            with open(meta) as f:
                m = json.load(f)
            out.append({"name": name, "title": m["title"], "expect": m["expect"], "why": m["why"]})
    return out


@app.get("/api/samples/{name}")
def sample_detail(name: str):
    d = os.path.join(SAMPLES, os.path.basename(name))
    if not os.path.isdir(d):
        raise HTTPException(404)
    with open(os.path.join(d, "payload.json")) as f:
        payload = json.load(f)
    files = [fn for fn in os.listdir(d) if fn.endswith(".pdf")]
    return {"payload": payload, "files": files}


@app.get("/api/samples/{name}/file/{fn}")
def sample_file(name: str, fn: str):
    path = os.path.join(SAMPLES, os.path.basename(name), os.path.basename(fn))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf")


@app.post("/api/samples/{name}/run")
def run_sample(name: str):
    d = os.path.join(SAMPLES, os.path.basename(name))
    with open(os.path.join(d, "payload.json")) as f:
        payload = json.load(f)
    docs = {fn.split("__")[0]: os.path.join(d, fn) for fn in os.listdir(d) if fn.endswith(".pdf")}
    return start(payload, docs)


@app.get("/api/runs")
def runs():
    return engine.list_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: str):
    r = engine.load_run(run_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/api/stats")
def stats():
    rs = engine.list_runs()
    by = {s: sum(r["status"] == s for r in rs) for s in ("approved", "pending", "rejected", "running")}
    return {"total": len(rs), **by}


@app.post("/api/reset")
def reset():
    """Wipe run history and re-seed the vendor master (use before a live demo)."""
    with engine.db() as con:
        con.execute("DROP TABLE IF EXISTS runs")
        for t in ("vendor_master", "vendor_registry", "run_secrets"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    engine.init_db()
    return {"ok": True}


app.mount("/",StaticFiles(directory=os.path.join(BASE, "static"), html=True), name="static")
