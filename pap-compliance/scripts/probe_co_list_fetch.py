#!/usr/bin/env python3
"""
Confirm the list+fetch model for CO reports. Hits the two endpoints
found in the Angular JS source and dumps everything.
"""
import json, sys, os, base64, time, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", "/tmp/pap/pap_creds.json")
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
OUT = os.environ.get("OUT", "/tmp/pap/co_list_fetch_probe.json")
HUTCHINS_UUID = "8a60d3e6-4fde-4596-a331-6b3ea519519f"

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, dict):
        for k, v in data.items():
            vs = str(v)
            if len(vs) > 400: vs = vs[:400] + "..."
            print(f"  {k}: {vs}", flush=True)
    elif isinstance(data, list):
        for i, item in enumerate(data[:20]):
            print(f"  [{i}] {json.dumps(item, default=str)[:300]}", flush=True)
        if len(data) > 20:
            print(f"  ... ({len(data)} total)", flush=True)
    else:
        print(f"  {data}", flush=True)
    report["steps"].append({"step": step, "data": data})

def co_secret_key(offset=713):
    now = datetime.now(timezone.utc)
    adj = now - timedelta(seconds=offset)
    iso = adj.strftime('%Y-%m-%dT%H:%M:%S.') + f"{adj.microsecond // 1000:03d}Z"
    return iso[5:21]

def co_encrypt_password(pw, offset=713):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    k = co_secret_key(offset).encode('utf-8')
    return base64.b64encode(AES.new(k, AES.MODE_CBC, k).encrypt(pad(pw.encode('utf-8'), 16))).decode()

def do_login(sess, username, password):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    r = sess.post(f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins", json={
        "username": username, "encryptedPassword": co_encrypt_password(password),
        "applicationId": "Sapphire", "timeStamp": ts,
    }, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
    return r.json()

with open(CREDS_PATH) as f:
    creds = json.load(f)
username = creds["CareOrchestrator"]["username"]
password = creds["CareOrchestrator"]["password"]

s = requests.Session()
s.headers.update({"User-Agent": UA})

# Auth dance with retry (fresh ts each attempt)
for attempt in range(3):
    auth = do_login(s, username, password)
    if "token" in auth:
        break
    time.sleep(2)
token = auth["token"]
org_id = auth.get("userTopOrgId")
hdrs = {"Accept": "application/json", "Content-Type": "application/json",
        "auth_token": json.dumps(token)}
s.post(f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
       json={"orgId": org_id}, headers=hdrs, timeout=30)
auth2 = do_login(s, username, password)
token2 = auth2.get("token", token)
hdrs = {"Accept": "application/json", "Content-Type": "application/json",
        "auth_token": json.dumps(token2)}
log("1. auth complete", {"org_id": org_id})

# ================================================================
# TEST 1: List documents for HUTCHINS, KEVIN
# ================================================================
list_url = f"{CO_BASE}/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}"
r = s.get(list_url, headers=hdrs, timeout=30)
log("2. list documents for HUTCHINS", {
    "url": list_url,
    "status": r.status_code,
    "content_type": r.headers.get("Content-Type"),
    "size": len(r.text),
    "snippet": r.text[:1000],
})

docs = []
if r.status_code == 200:
    try:
        data = r.json()
        if isinstance(data, list):
            docs = data
        elif isinstance(data, dict):
            docs = data.get("documents", data.get("data", data.get("content", [])))
            if not isinstance(docs, list):
                docs = [data]
        log("2a. parsed documents", {"count": len(docs), "type": type(data).__name__})
        # Dump EVERY document's full metadata
        for i, doc in enumerate(docs):
            log(f"2b. document [{i}]", doc)
    except Exception as e:
        log("2a. parse error", {"error": str(e), "body": r.text[:500]})

# ================================================================
# TEST 2: Try fetching the first document (if any)
# ================================================================
if docs:
    # Find document ID field — try common names
    first_doc = docs[0]
    doc_id = (first_doc.get("documentId") or first_doc.get("id")
              or first_doc.get("objectId") or first_doc.get("_id"))
    if doc_id:
        fetch_url = f"{CO_BASE}/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}/document/{doc_id}"
        r2 = s.get(fetch_url, headers=hdrs, timeout=60)
        is_pdf = r2.content[:5] == b"%PDF-"
        log("3. fetch document", {
            "url": fetch_url,
            "doc_id": doc_id,
            "status": r2.status_code,
            "content_type": r2.headers.get("Content-Type"),
            "size": len(r2.content),
            "is_pdf": is_pdf,
            "first_20_hex": r2.content[:20].hex() if r2.content else "",
            "body_snippet": r2.text[:200] if not is_pdf else f"<PDF {len(r2.content)} bytes>",
        })
        if is_pdf:
            with open("/tmp/pap/co_hutchins_doc.pdf", "wb") as f:
                f.write(r2.content)
            log("3a. PDF SAVED!", {"path": "/tmp/pap/co_hutchins_doc.pdf", "size": len(r2.content)})
    else:
        log("3. no documentId field found", {"keys": list(first_doc.keys())})
else:
    log("3. no documents to fetch", None)

# ================================================================
# TEST 3: Try a second patient from wildcard (broader confirmation)
# ================================================================
wc = s.get(f"{CO_BASE}/proxy/patientgateway-v1-server/patient/search/wildcard",
           params={"page": "1", "pageSize": "10", "sortBy": "lastName",
                   "sortOrder": "asc", "active": "true", "inactive": "false"},
           headers=hdrs, timeout=30)
try:
    pts = wc.json() if wc.status_code == 200 else []
except:
    pts = []

for pt in pts[:5]:
    pid = pt.get("patientId")
    if pid == HUTCHINS_UUID:
        continue
    pname = f"{pt.get('lastName','')}, {pt.get('firstName','')}"
    r3 = s.get(f"{CO_BASE}/proxy/documents-v1-0-server/patients/{pid}",
               headers=hdrs, timeout=30)
    if r3.status_code == 200:
        try:
            d3 = r3.json()
            count = len(d3) if isinstance(d3, list) else "?"
        except:
            count = "parse_err"
        log(f"4. list docs for {pname}", {"uuid": pid, "status": r3.status_code,
                                           "doc_count": count,
                                           "snippet": r3.text[:300]})
        break

# Save
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. Full capture at {OUT}")
print("Return ALL terminal output AND the full contents of that file.")
print("Critical: the document metadata fields (step 2b) and whether")
print("the PDF fetch (step 3) actually returns a PDF.")
