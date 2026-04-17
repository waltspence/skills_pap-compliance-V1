#!/usr/bin/env python3
"""
Final targeted probe before HAR capture. Tests two hypotheses:

1. Missing patient context: the Angular app navigates to a patient before
   generating reports, which may establish server-side state. We replicate
   the calls the SPA would make: patient detail, equipment, therapy data.

2. Wrong path shape: /reports/generate may need a path parameter like
   /{patientId} or /{templateId}/generate.

Also scans for alternate report-generation service names.
"""
import json, sys, os, base64, time, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", os.environ.get("CREDS", "/tmp/pap/pap_creds.json"))
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
OUT = os.environ.get("OUT", "/tmp/pap/co_final_probe.json")
PATIENT_UUID = "8a60d3e6-4fde-4596-a331-6b3ea519519f"
SERIAL = "D134783924AE19"
TEMPLATE_ID = "ebedbf1a-be12-4756-9661-85dc7bec1792"

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, dict):
        for k, v in data.items():
            vs = str(v); print(f"  {k}: {vs[:300]}", flush=True)
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

# Auth with retry (fresh timestamp each attempt)
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
# HYPOTHESIS 1: Patient context — replicate the SPA navigation calls
# ================================================================

# The Angular app navigates to /#/patient/{uuid}/therapydata/reports
# before showing the generate button. That route triggers these API calls:

# a. Patient detail
r = s.get(f"{CO_BASE}/proxy/patientgateway-v1-server/patient/{PATIENT_UUID}",
          headers=hdrs, timeout=30)
log("2a. patient detail", {"status": r.status_code, "size": len(r.text),
    "snippet": r.text[:300]})

# b. Equipment
r = s.get(f"{CO_BASE}/proxy/equipment-v1-0-server/patient/{PATIENT_UUID}/equipment",
          headers=hdrs, timeout=30)
log("2b. equipment", {"status": r.status_code, "snippet": r.text[:300]})

# c. Therapy data endpoints the SPA might hit
therapy_paths = [
    f"/proxy/patientgateway-v1-server/patient/{PATIENT_UUID}/therapydata",
    f"/proxy/patientgateway-v1-server/patient/{PATIENT_UUID}/compliance",
    f"/proxy/patientgateway-v1-server/patient/{PATIENT_UUID}/therapy",
    f"/proxy/documents-v1-0-server/patient/{PATIENT_UUID}/reports",
    f"/proxy/documents-v1-0-server/reports?patientId={PATIENT_UUID}",
    f"/proxy/documents-v1-0-server/reports?objectId={PATIENT_UUID}",
    f"/proxy/therapyreporttemplates-v1-0-server/api/v1/reports?patientId={PATIENT_UUID}",
]
for path in therapy_paths:
    try:
        r = s.get(f"{CO_BASE}{path}", headers=hdrs, timeout=15)
        interesting = r.status_code == 200 and len(r.content) > 2
        log(f"2c. GET {path}{'  ← HAS DATA' if interesting else ''}",
            {"status": r.status_code, "size": len(r.content),
             "snippet": r.text[:300] if interesting else r.text[:100]})
    except Exception as e:
        log(f"2c. GET {path}", {"error": str(e)})

# NOW try generate — if patient context was needed, it should be set
end_dt = datetime.now()
start_dt = end_dt - timedelta(days=30)
body = {
    "templateId": TEMPLATE_ID,
    "patientId": PATIENT_UUID,
    "deviceSerialNumber": SERIAL,
    "startDate": start_dt.strftime("%Y-%m-%d"),
    "endDate": end_dt.strftime("%Y-%m-%d"),
}

url = f"{CO_BASE}/proxy/documents-v1-0-server/reports/generate"
r = s.post(url, json=body, headers=hdrs, timeout=60)
different = r.status_code != 400
log(f"3. POST generate AFTER patient context{'  ← DIFFERENT!' if different else ''}",
    {"status": r.status_code, "size": len(r.content),
     "content_type": r.headers.get("Content-Type"),
     "snippet": r.text[:500] if r.content[:5] != b"%PDF-" else f"<PDF {len(r.content)} bytes>",
     "is_pdf": r.content[:5] == b"%PDF-"})

# ================================================================
# HYPOTHESIS 2: Path shape variants
# ================================================================

path_variants = [
    f"/proxy/documents-v1-0-server/reports/generate/{PATIENT_UUID}",
    f"/proxy/documents-v1-0-server/reports/generate/{TEMPLATE_ID}",
    f"/proxy/documents-v1-0-server/reports/{PATIENT_UUID}/generate",
    f"/proxy/documents-v1-0-server/reports/{TEMPLATE_ID}/generate",
    f"/proxy/documents-v1-0-server/patient/{PATIENT_UUID}/reports/generate",
    f"/proxy/documents-v1-0-server/{PATIENT_UUID}/reports/generate",
    f"/proxy/documents-v1-0-server/reports/generate?patientId={PATIENT_UUID}",
    f"/proxy/documents-v1-0-server/reports/generate?templateId={TEMPLATE_ID}&patientId={PATIENT_UUID}",
]

for path in path_variants:
    try:
        r = s.post(f"{CO_BASE}{path}", json=body, headers=hdrs, timeout=30)
        different = r.status_code != 400
        log(f"4. POST {path}{'  ← DIFFERENT!' if different else ''}",
            {"status": r.status_code, "size": len(r.content),
             "content_type": r.headers.get("Content-Type"),
             "snippet": r.text[:200]})
    except Exception as e:
        log(f"4. POST {path}", {"error": str(e)})

# ================================================================
# SCAN: Alternate service names for report generation
# ================================================================

service_scans = [
    "/proxy/reports-v1-0-server/reports/generate",
    "/proxy/reports-v1-0-server/generate",
    "/proxy/reportgeneration-v1-0-server/reports/generate",
    "/proxy/sleeptrend-v1-0-server/reports/generate",
    "/proxy/therapyreports-v1-0-server/reports/generate",
    "/proxy/therapyreports-v1-0-server/api/v1/reports/generate",
    "/proxy/pdfgenerator-v1-0-server/reports/generate",
    "/proxy/documents-v1-0-server/generate",
    "/proxy/documents-v1-0-server/api/v1/reports/generate",
    "/api/reports/generate",
    "/api/v1/reports/generate",
]

for path in service_scans:
    try:
        r = s.post(f"{CO_BASE}{path}", json=body, headers=hdrs, timeout=10)
        interesting = r.status_code not in (404, 400)
        log(f"5. POST {path}{'  ← INTERESTING!' if interesting else ''}",
            {"status": r.status_code, "snippet": r.text[:150]})
    except Exception as e:
        log(f"5. POST {path}", {"error": str(e)})

# Save
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. Full capture at {OUT}")
print("Return the contents of that file plus all terminal output above.")
