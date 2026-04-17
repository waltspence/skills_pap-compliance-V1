#!/usr/bin/env python3
"""
Probe 9: find the actual PDF download path for a CO document.
The /document/{id} endpoint returns JSON metadata, not PDF bytes.
Try all likely path/header variants to get the actual PDF.
"""
import json, sys, os, re, base64, time, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", "/tmp/pap/pap_creds.json")
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
OUT = os.environ.get("OUT", "/tmp/pap/co_pdf_fetch_probe.json")

HUTCHINS_UUID = "8a60d3e6-4fde-4596-a331-6b3ea519519f"
DOC_ID = "01ba86a2-304a-498c-9623-c24b5e120c13"

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, dict):
        for k, v in data.items():
            vs = str(v)
            if len(vs) > 400: vs = vs[:400] + "..."
            print(f"  {k}: {vs}", flush=True)
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

base_doc = f"/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}/document/{DOC_ID}"

def try_get(label, path, extra_headers=None, params=None):
    url = f"{CO_BASE}{path}"
    h = {**hdrs, **(extra_headers or {})}
    try:
        r = s.get(url, headers=h, params=params, timeout=60)
        is_pdf = r.content[:5] == b"%PDF-"
        different = r.status_code != 200 or is_pdf or r.headers.get("Content-Type", "") != "application/json"
        snap = {
            "url": url,
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type"),
            "content_length": r.headers.get("Content-Length"),
            "size": len(r.content),
            "is_pdf": is_pdf,
            "first_20_hex": r.content[:20].hex() if r.content else "",
            "body_snippet": r.text[:300] if not is_pdf else f"<PDF {len(r.content)} bytes>",
        }
        marker = " ← PDF!!!" if is_pdf else (" ← DIFFERENT" if different else "")
        log(f"{label}{marker}", snap)
        if is_pdf:
            with open("/tmp/pap/co_hutchins_compliance.pdf", "wb") as f:
                f.write(r.content)
            log(f"  PDF SAVED!", {"path": "/tmp/pap/co_hutchins_compliance.pdf", "size": len(r.content)})
            return True
    except Exception as e:
        log(f"{label} EXCEPTION", {"error": f"{type(e).__name__}: {e}"})
    return False

# ================================================================
# GROUP 1: Path suffixes on the document endpoint
# ================================================================
suffixes = ["/content", "/download", "/pdf", "/file", "/binary", "/stream",
            "/presigned", "/url", "/signed-url"]
for suf in suffixes:
    if try_get(f"2. {base_doc}{suf}", f"{base_doc}{suf}"):
        break

# ================================================================
# GROUP 2: Accept header content negotiation on the base endpoint
# ================================================================
accept_variants = [
    ("application/pdf", {"Accept": "application/pdf"}),
    ("application/octet-stream", {"Accept": "application/octet-stream"}),
    ("*/*", {"Accept": "*/*"}),
    ("application/pdf + no content-type", {"Accept": "application/pdf", "Content-Type": ""}),
]
for label, extra in accept_variants:
    if try_get(f"3. base doc Accept: {label}", base_doc, extra_headers=extra):
        break

# ================================================================
# GROUP 3: Presigned URL endpoint (from care_orchestrator.md)
# ================================================================
presigned_paths = [
    f"/proxy/documents-v1-0-server/reports/presigned?objectId={HUTCHINS_UUID}&documentId={DOC_ID}",
    f"/proxy/documents-v1-0-server/presigned?objectId={HUTCHINS_UUID}&documentId={DOC_ID}",
    f"/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}/presigned?documentId={DOC_ID}",
    f"/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}/document/{DOC_ID}/presigned",
]
for path in presigned_paths:
    url = f"{CO_BASE}{path}"
    try:
        r = s.get(url, headers=hdrs, timeout=30)
        has_url = False
        if r.status_code == 200:
            try:
                js = r.json()
                purl = js.get("presignedUrl") or js.get("url") or js.get("downloadUrl") or js.get("signedUrl")
                if purl:
                    has_url = True
                    log(f"4. presigned → URL found!", {"path": path, "presigned_url": purl[:200]})
                    pr = s.get(purl, timeout=60)
                    if pr.content[:5] == b"%PDF-":
                        with open("/tmp/pap/co_hutchins_compliance.pdf", "wb") as f:
                            f.write(pr.content)
                        log("4a. PDF from presigned!", {"size": len(pr.content)})
                    else:
                        log("4a. presigned content not PDF", {"status": pr.status_code,
                            "size": len(pr.content), "hex": pr.content[:20].hex()})
            except:
                pass
        if not has_url:
            log(f"4. presigned {path}", {"status": r.status_code, "snippet": r.text[:200]})
    except Exception as e:
        log(f"4. presigned EXCEPTION", {"error": str(e)})

# ================================================================
# GROUP 5: reportgateway and legacydevicereports (from JS config)
# ================================================================
alt_services = [
    f"/proxy/reportgateway-v1-server/reports/{DOC_ID}",
    f"/proxy/reportgateway-v1-server/reports/{HUTCHINS_UUID}/{DOC_ID}",
    f"/proxy/reportgateway-v1-server/reports/download/{DOC_ID}",
    f"/proxy/reportgateway-v1-server/reports/content/{DOC_ID}",
    f"/proxy/reportgateway-v1-server/patients/{HUTCHINS_UUID}/reports/{DOC_ID}",
    f"/proxy/legacydevicereports-v1-0-server/legacy/reports/{DOC_ID}",
    f"/proxy/legacydevicereports-v1-0-server/legacy/reports/{HUTCHINS_UUID}/{DOC_ID}",
]
for path in alt_services:
    if try_get(f"5. {path}", path):
        break

# ================================================================
# GROUP 6: Query-param style matching getComplianceReport signature
# getComplianceReport(patientId, documentId, startDate, endDate, stod)
# ================================================================
qp_paths = [
    (f"/proxy/documents-v1-0-server/reports",
     {"patientId": HUTCHINS_UUID, "documentId": DOC_ID,
      "startDate": "2026-01-28", "endDate": "2026-02-26"}),
    (f"/proxy/documents-v1-0-server/compliance-report",
     {"patientId": HUTCHINS_UUID, "documentId": DOC_ID,
      "startDate": "2026-01-28", "endDate": "2026-02-26"}),
    (f"/proxy/documents-v1-0-server/patients/{HUTCHINS_UUID}/compliance-report",
     {"documentId": DOC_ID, "startDate": "2026-01-28", "endDate": "2026-02-26"}),
]
for path, params in qp_paths:
    url = f"{CO_BASE}{path}"
    try:
        r = s.get(url, headers=hdrs, params=params, timeout=30)
        is_pdf = r.content[:5] == b"%PDF-"
        log(f"6. GET {path} + params{'  ← PDF!' if is_pdf else ''}",
            {"status": r.status_code, "size": len(r.content),
             "content_type": r.headers.get("Content-Type"),
             "snippet": r.text[:200] if not is_pdf else f"<PDF {len(r.content)} bytes>"})
        if is_pdf:
            with open("/tmp/pap/co_hutchins_compliance.pdf", "wb") as f:
                f.write(r.content)
            log("6a. PDF SAVED!", {"size": len(r.content)})
            break
    except Exception as e:
        log(f"6. EXCEPTION", {"error": str(e)})

# ================================================================
# GROUP 7: Extract getComplianceReport from main.js for ground truth
# ================================================================
log("7. Fetching main.js to extract getComplianceReport...", None)
try:
    r = s.get(f"{CO_BASE}/main.139a19415271de77.js", timeout=60)
    if r.status_code == 200:
        js = r.text
        # Find getComplianceReport function
        patterns = [
            r'getComplianceReport\s*\([^)]*\)\s*\{[^}]{0,2000}\}',
            r'getComplianceReport[^{]{0,200}\{[^}]{0,2000}',
            r'complianceReport[^;]{0,500}',
            r'openPdf[^}]{0,1500}',
            r'contentUrl[^;]{0,300}',
        ]
        for pat in patterns:
            for m in re.finditer(pat, js):
                ctx = js[max(0, m.start()-100):m.end()+100]
                log(f"7a. JS match [{pat[:30]}...]", ctx[:600])
        # Also search for the documents base path usage
        for m in re.finditer(r'apiUriPatientGetDocument[^}]{0,500}', js):
            ctx = js[max(0, m.start()-200):m.end()+200]
            log("7b. apiUriPatientGetDocument usage", ctx[:600])
        for m in re.finditer(r'documents-v1-0-server[^"]{0,100}', js):
            ctx = js[max(0, m.start()-100):m.end()+100]
            log("7c. documents-v1-0-server ref", ctx[:400])
    else:
        log("7. main.js fetch failed", {"status": r.status_code})
except Exception as e:
    log("7. main.js EXCEPTION", {"error": str(e)})

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. Full capture at {OUT}")
print("Return ALL terminal output AND the full contents of that file.")
print("Most important: did ANY path return PDF bytes? And what does")
print("getComplianceReport actually do in the JS source?")
