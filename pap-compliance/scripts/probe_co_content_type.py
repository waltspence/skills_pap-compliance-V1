#!/usr/bin/env python3
"""
Targeted probe: the CORS whitelist only allows x-requested-with and auth_token.
Content-Type: application/json is NOT whitelisted and likely causes the 400.
Try sending the JSON body with various Content-Type values or none at all.
"""
import json, sys, os, base64, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", "/tmp/pap/pap_creds.json")
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
OUT = "/tmp/pap/co_content_type_probe.json"

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, dict):
        for k, v in data.items():
            vs = str(v)
            if len(vs) > 300: vs = vs[:300] + "..."
            print(f"  {k}: {vs}", flush=True)
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

with open(CREDS_PATH) as f:
    creds = json.load(f)
username = creds["CareOrchestrator"]["username"]
password = creds["CareOrchestrator"]["password"]

s = requests.Session()
s.headers.update({"User-Agent": UA})

# Auth dance
ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
login_url = f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins"
for attempt in range(3):
    r = s.post(login_url, json={
        "username": username, "encryptedPassword": co_encrypt_password(password),
        "applicationId": "Sapphire", "timeStamp": ts,
    }, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
    try:
        auth = r.json()
        if "token" in auth:
            break
    except:
        pass
    import time; time.sleep(2)

token = auth["token"]
org_id = auth.get("userTopOrgId")
log("1. login", {"status": r.status_code, "org_id": org_id})

auth_hdrs = {"Accept": "application/json", "Content-Type": "application/json",
             "auth_token": json.dumps(token)}
s.post(f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
       json={"orgId": org_id}, headers=auth_hdrs, timeout=30)

ts2 = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
r2 = s.post(login_url, json={
    "username": username, "encryptedPassword": co_encrypt_password(password),
    "applicationId": "Sapphire", "timeStamp": ts2,
}, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
auth2 = r2.json()
token2 = auth2.get("token", token)
token_json = json.dumps(token2)
log("2. re-login", {"status": r2.status_code})

# Target patient: HUTCHINS, KEVIN — confirmed has serial D134783924AE19
PATIENT_UUID = "8a60d3e6-4fde-4596-a331-6b3ea519519f"
SERIAL = "D134783924AE19"
TEMPLATE_ID = "ebedbf1a-be12-4756-9661-85dc7bec1792"

end_dt = datetime.now()
start_dt = end_dt - timedelta(days=30)
body_dict = {
    "templateId": TEMPLATE_ID,
    "patientId": PATIENT_UUID,
    "deviceSerialNumber": SERIAL,
    "startDate": start_dt.strftime("%Y-%m-%d"),
    "endDate": end_dt.strftime("%Y-%m-%d"),
}
body_json = json.dumps(body_dict)

url = f"{CO_BASE}/proxy/documents-v1-0-server/reports/generate"

# ================================================================
# The key hypothesis: Content-Type: application/json is rejected by
# the CORS middleware. The whitelist only allows x-requested-with and
# auth_token. Try sending the same JSON body with different
# Content-Type values or none at all.
# ================================================================

attempts = [
    # 1. ONLY auth_token header, no Content-Type, no Accept — bare minimum
    ("bare_auth_only", {
        "auth_token": token_json,
    }),

    # 2. auth_token + Content-Type: text/plain (CORS-safe)
    ("text_plain", {
        "auth_token": token_json,
        "Content-Type": "text/plain",
    }),

    # 3. auth_token + x-requested-with (both whitelisted), no Content-Type
    ("whitelisted_only", {
        "auth_token": token_json,
        "x-requested-with": "XMLHttpRequest",
    }),

    # 4. auth_token + x-requested-with + Content-Type: text/plain
    ("whitelisted_plus_text", {
        "auth_token": token_json,
        "x-requested-with": "XMLHttpRequest",
        "Content-Type": "text/plain",
    }),

    # 5. Form-urlencoded body (completely different encoding)
    ("form_urlencoded", {
        "auth_token": token_json,
        "Content-Type": "application/x-www-form-urlencoded",
    }),

    # 6. auth_token + Accept: application/pdf (tell server we want PDF)
    ("accept_pdf", {
        "auth_token": token_json,
        "Accept": "application/pdf",
    }),

    # 7. auth_token + Accept: application/pdf + Content-Type: text/plain
    ("accept_pdf_text_plain", {
        "auth_token": token_json,
        "Accept": "application/pdf",
        "Content-Type": "text/plain",
    }),

    # 8. Baseline (what we've been sending) — for comparison
    ("baseline_json", {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "auth_token": token_json,
    }),
]

for label, hdrs in attempts:
    is_form = hdrs.get("Content-Type") == "application/x-www-form-urlencoded"
    try:
        if is_form:
            # Send as form-encoded key=value pairs
            r = s.post(url, data=body_dict, headers=hdrs, timeout=60)
        else:
            # Send JSON string as raw body (not using json= which forces Content-Type)
            r = s.post(url, data=body_json.encode("utf-8"), headers=hdrs, timeout=60)
        is_pdf = r.content[:5] == b"%PDF-"
        different = r.status_code != 400
        snap = {
            "headers_sent": hdrs,
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type"),
            "content_length": r.headers.get("Content-Length"),
            "body_size": len(r.content),
            "body_snippet": r.text[:500] if not is_pdf else f"<PDF {len(r.content)} bytes>",
            "body_hex": r.content[:40].hex() if r.content else "",
            "is_pdf": is_pdf,
            "cors_allow_headers": r.headers.get("Access-Control-Allow-Headers"),
            "all_response_headers": dict(r.headers),
        }
        marker = " ← DIFFERENT!" if different else ""
        marker = " ← PDF!!!" if is_pdf else marker
        log(f"3.{label}{marker}", snap)

        if is_pdf:
            with open("/tmp/pap/co_hutchins_sleep_trend.pdf", "wb") as f:
                f.write(r.content)
            log("PDF SAVED!", {"path": "/tmp/pap/co_hutchins_sleep_trend.pdf",
                               "size": len(r.content)})
            break
        if different:
            log(f"  Status {r.status_code} != 400 — this header set changes behavior", None)
    except Exception as e:
        log(f"3.{label} EXCEPTION", {"error": f"{type(e).__name__}: {e}"})

# Also try an OPTIONS preflight to see what the server actually wants
try:
    r = s.options(url, headers={"auth_token": token_json,
                                 "Origin": "https://www.careorchestrator.com",
                                 "Access-Control-Request-Method": "POST",
                                 "Access-Control-Request-Headers": "auth_token,content-type"},
                  timeout=30)
    log("4. OPTIONS preflight", {
        "status": r.status_code,
        "allow_headers": r.headers.get("Access-Control-Allow-Headers"),
        "allow_methods": r.headers.get("Access-Control-Allow-Methods"),
        "allow_origin": r.headers.get("Access-Control-Allow-Origin"),
        "max_age": r.headers.get("Access-Control-Max-Age"),
        "all_headers": dict(r.headers),
    })
except Exception as e:
    log("4. OPTIONS EXCEPTION", {"error": str(e)})

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. Full capture at {OUT}")
print(f"Return the contents of that file plus all terminal output above.")
