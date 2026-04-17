#!/usr/bin/env python3
"""
Focused probe: fetch the full templates list from therapyreporttemplates service,
dump the Sleep Trend template's metadata (which likely specifies required generate
fields), then try generating via BOTH the old and new service paths.
"""
import json, sys, os, base64, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", "/tmp/pap/pap_creds.json")
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
SLEEP_TREND_ID = "ebedbf1a-be12-4756-9661-85dc7bec1792"
OUT = "/tmp/pap/co_templates_probe.json"

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, dict):
        for k, v in data.items():
            vs = str(v)
            if len(vs) > 300: vs = vs[:300] + "..."
            print(f"  {k}: {vs}", flush=True)
    elif isinstance(data, list):
        for item in data[:10]:
            print(f"  {json.dumps(item, default=str)[:300]}", flush=True)
        if len(data) > 10:
            print(f"  ... ({len(data)} total)", flush=True)
    else:
        print(f"  {data}", flush=True)
    report["steps"].append({"step": step, "data": data})

def co_secret_key(offset=713):
    now = datetime.now(timezone.utc)
    adjusted = now - timedelta(seconds=offset)
    iso = adjusted.strftime('%Y-%m-%dT%H:%M:%S.') + f"{adjusted.microsecond // 1000:03d}Z"
    return iso[5:21]

def co_encrypt_password(password, offset=713):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    k = co_secret_key(offset).encode('utf-8')
    return base64.b64encode(AES.new(k, AES.MODE_CBC, k).encrypt(pad(password.encode('utf-8'), 16))).decode()

# Load creds
with open(CREDS_PATH) as f:
    creds = json.load(f)
username = creds["CareOrchestrator"]["username"]
password = creds["CareOrchestrator"]["password"]

s = requests.Session()
s.headers.update({"User-Agent": UA})

# Auth dance: login → context → re-login
ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
login_url = f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins"
r = s.post(login_url, json={
    "username": username, "encryptedPassword": co_encrypt_password(password),
    "applicationId": "Sapphire", "timeStamp": ts,
}, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
auth = r.json()
token = auth["token"]
org_id = auth.get("userTopOrgId")
hdrs = {"Accept": "application/json", "Content-Type": "application/json",
        "auth_token": json.dumps(token)}
log("1. login", {"status": r.status_code, "org_id": org_id})

s.post(f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
       json={"orgId": org_id}, headers=hdrs, timeout=30)

ts2 = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
r2 = s.post(login_url, json={
    "username": username, "encryptedPassword": co_encrypt_password(password),
    "applicationId": "Sapphire", "timeStamp": ts2,
}, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
auth2 = r2.json()
token2 = auth2.get("token", token)
hdrs = {"Accept": "application/json", "Content-Type": "application/json",
        "auth_token": json.dumps(token2)}
log("2. re-login post-context", {"status": r2.status_code})

# ================================================================
# PROBE 1: Full templates list — dump EVERYTHING
# ================================================================
r = s.get(f"{CO_BASE}/proxy/therapyreporttemplates-v1-0-server/api/v1/reports/templates",
          headers=hdrs, timeout=30)
log("3. templates list status", {"status": r.status_code, "content_type": r.headers.get("Content-Type")})

if r.status_code == 200:
    try:
        templates = r.json()
        log("3a. templates count", {"count": len(templates) if isinstance(templates, list) else "not a list"})

        # Dump FULL response — every field of every template
        report["full_templates_response"] = templates
        if isinstance(templates, list):
            for t in templates:
                tid = t.get("templateId") or t.get("id") or "?"
                name = t.get("name") or t.get("templateName") or "?"
                log(f"3b. template: {name}", t)

            # Find Sleep Trend specifically
            st = [t for t in templates if SLEEP_TREND_ID in json.dumps(t)]
            if st:
                log("3c. SLEEP TREND TEMPLATE (full dump)", st[0])
            else:
                log("3c. Sleep Trend ID not found in templates", {"searched_for": SLEEP_TREND_ID})
        else:
            log("3a. templates response (not a list)", {"type": type(templates).__name__,
                                                         "dump": json.dumps(templates, default=str)[:1000]})
    except Exception as e:
        log("3a. templates parse error", {"error": str(e), "body": r.text[:500]})

# ================================================================
# PROBE 2: Find a patient with a serial
# ================================================================
wc = s.get(f"{CO_BASE}/proxy/patientgateway-v1-server/patient/search/wildcard",
           params={"page": "1", "pageSize": "50", "sortBy": "lastName",
                   "sortOrder": "asc", "active": "true", "inactive": "false"},
           headers=hdrs, timeout=30)
pts = wc.json() if wc.status_code == 200 and isinstance(wc.json(), list) else []

probe_uuid = None
serial = None
for pt in pts:
    pid = pt.get("patientId")
    er = s.get(f"{CO_BASE}/proxy/equipment-v1-0-server/patient/{pid}/equipment",
               headers=hdrs, timeout=30)
    if er.status_code == 200:
        try:
            eq = er.json()
            if isinstance(eq, list):
                for e in eq:
                    sn = e.get("serialNumber")
                    if sn:
                        probe_uuid = pid
                        serial = sn
                        log("4. patient with serial", {"uuid": pid,
                            "name": f"{pt.get('lastName','')}, {pt.get('firstName','')}",
                            "serial": sn, "equipmentType": e.get("equipmentType"),
                            "full_equipment": e})
                        break
        except: pass
    if serial:
        break

if not probe_uuid:
    probe_uuid = pts[0].get("patientId") if pts else None
    log("4. no patient with serial in 50 — using first", {"uuid": probe_uuid})

# ================================================================
# PROBE 3: Try OLD therapyreporttemplates generate endpoint
# (was 502 previously — may have been fixed, and has better errors)
# ================================================================
if probe_uuid:
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=30)
    body = {
        "templateId": SLEEP_TREND_ID,
        "patientId": probe_uuid,
        "startDate": start_dt.strftime("%Y-%m-%d"),
        "endDate": end_dt.strftime("%Y-%m-%d"),
    }
    if serial:
        body["deviceSerialNumber"] = serial

    # Old path
    old_url = f"{CO_BASE}/proxy/therapyreporttemplates-v1-0-server/api/v1/reports/generate"
    try:
        r = s.post(old_url, json=body, headers=hdrs, timeout=60)
        log("5. OLD generate endpoint", {"status": r.status_code,
            "content_type": r.headers.get("Content-Type"),
            "content_length": r.headers.get("Content-Length"),
            "body_snippet": r.text[:500] if r.content[:5] != b"%PDF-" else f"<PDF {len(r.content)} bytes>",
            "is_pdf": r.content[:5] == b"%PDF-",
            "all_response_headers": dict(r.headers)})
    except Exception as e:
        log("5. OLD generate EXCEPTION", {"error": str(e)})

    # New path with same body
    new_url = f"{CO_BASE}/proxy/documents-v1-0-server/reports/generate"
    try:
        r = s.post(new_url, json=body, headers=hdrs, timeout=60)
        log("6. NEW generate endpoint (baseline)", {"status": r.status_code,
            "content_type": r.headers.get("Content-Type"),
            "content_length": r.headers.get("Content-Length"),
            "body_snippet": r.text[:500] if r.content[:5] != b"%PDF-" else f"<PDF {len(r.content)} bytes>",
            "is_pdf": r.content[:5] == b"%PDF-",
            "all_response_headers": dict(r.headers)})
    except Exception as e:
        log("6. NEW generate EXCEPTION", {"error": str(e)})

    # ================================================================
    # PROBE 4: If templates gave us field hints, try enhanced body
    # ================================================================
    # Try with orgId, reportFamily, templateClass, context
    enhanced_bodies = [
        ("+ orgId", {**body, "orgId": org_id}),
        ("+ orgId + reportFamily", {**body, "orgId": org_id, "reportFamily": 1}),
        ("+ orgId + family 6", {**body, "orgId": org_id, "reportFamily": 6}),
        ("+ context", {**body, "context": "therapy-reports"}),
        ("+ templateClass=trend", {**body, "templateClass": "trend"}),
        ("+ all guesses", {**body, "orgId": org_id, "reportFamily": 1,
                           "templateClass": "trend", "context": "therapy-reports",
                           "bestNumberOfDays": 30,
                           "fileName": "SleepTrend.pdf",
                           "reportComplianceByBlowerTime": False}),
    ]
    for label, eb in enhanced_bodies:
        try:
            r = s.post(new_url, json=eb, headers=hdrs, timeout=60)
            status_changed = r.status_code != 400
            log(f"7. NEW generate [{label}]{'  ← DIFFERENT!' if status_changed else ''}",
                {"status": r.status_code,
                 "content_type": r.headers.get("Content-Type"),
                 "content_length": r.headers.get("Content-Length"),
                 "body_snippet": r.text[:300],
                 "body_hex": r.content[:20].hex() if r.content else "",
                 "is_pdf": r.content[:5] == b"%PDF-"})
            if r.status_code == 200 and len(r.content) > 0:
                with open("/tmp/pap/co_generated.bin", "wb") as f:
                    f.write(r.content)
                log("  SAVED!", {"path": "/tmp/pap/co_generated.bin", "size": len(r.content)})
                break
        except Exception as e:
            log(f"7. [{label}] EXCEPTION", {"error": str(e)})

# Save
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. Full capture at {OUT}")
print(f"Return the contents of that file plus all terminal output above.")
