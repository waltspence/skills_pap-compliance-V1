# Care Orchestrator (Philips Respironics) Reference

**Report type:** Sleep Trend
**Base URL:** `https://www.careorchestrator.com`
**Auth:** AES-CBC encrypted password with time-based key, token-based API
**Transport:** Python `requests`

## Authentication

### Step 1: Encrypt password
Key = current UTC time minus 713 seconds, formatted as ISO substring. Key == IV.

```python
import base64
from datetime import datetime, timedelta, timezone
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

def secret_key():
    now = datetime.now(timezone.utc)
    adjusted = now - timedelta(seconds=713)
    iso = adjusted.strftime('%Y-%m-%dT%H:%M:%S.') + f"{adjusted.microsecond//1000:03d}Z"
    return iso[5:21]  # 16 chars, e.g. "03-25T15:56:19.3"

def encrypt_password(password):
    k = secret_key().encode('utf-8')
    return base64.b64encode(AES.new(k, AES.MODE_CBC, k).encrypt(pad(password.encode('utf-8'), 16))).decode()
```

Requires: `pip install pycryptodome --break-system-packages`

### Step 2: Login
```python
ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
r = session.post(f"{BASE}/proxy/sapphiregateway-v1-server/authentication/logins", json={
    "username": USERNAME,
    "encryptedPassword": encrypt_password(PASSWORD),
    "applicationId": "Sapphire",
    "timeStamp": ts,
}, headers={"Accept": "application/json", "Content-Type": "application/json"})
auth = r.json()
token = auth["token"]  # dict: userId, uniqueId, hash, timeToLive
org_id = auth["userTopOrgId"]
```

### Step 3: Auth header for all API calls
```python
headers = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "auth_token": json.dumps(token),  # full token object as JSON string
}
```

### Step 4: Set org context
```python
session.post(f"{BASE}/proxy/auth-v2-server/sessions/context",
    json={"orgId": org_id}, headers=headers)
# Returns 200 with empty body
```

**Note:** The `sapphiregateway-v1-server` is broken — all calls to it return 500 after
org context is set. Login still works through sapphiregateway (Step 2), but all subsequent
data calls must use the **`patientgateway-v1-server`** and **`equipment-v1-0-server`**
endpoints documented below. Re-login after context set to get a fresh token.

## Patient Search

**Use `patientgateway-v1-server`** — NOT `sapphiregateway-v1-server` (returns 500).

```python
r = session.get(f"{BASE}/proxy/patientgateway-v1-server/patient/search", params={
    "s": "LastName",
    "searchBy": "name",
    "page": "1",
    "pageSize": "50",
    "sortBy": "lastName",
    "sortOrder": "asc",
    "active": "true",
    "inactive": "false",
}, headers=headers)
```

Returns a **JSON array** directly (not wrapped in an object):
```json
[
  {
    "patientId": "uuid-here",
    "firstName": "Sharon",
    "lastName": "Barbini",
    "dateOfBirth": "1960-10-04",
    "active": true,
    "organization": {
      "orgName": "ABC Health Care",
      "orgDisplayName": "Apria Central, ABC Health Care",
      "orgId": "uuid"
    },
    "setupDate": "2020-02-25",
    "averageDaysUsed": "16/30",
    "averageHoursUsed": 3.15,
    "usagePercentage": 40,
    "email": "...",
    "phoneNumber": "...",
    "patientExternalId": "BT-xxxx-xxxxxx"
  }
]
```

**Key differences from sapphiregateway response:**
- Returns array, not `{"patientSearchResults": [...]}`.
- Patient ID field is `patientId`, not `uuid`.
- DOB format is `YYYY-MM-DD`.
- Includes `averageDaysUsed`, `averageHoursUsed`, `usagePercentage` inline.
- Does NOT include `deviceSerialNumber` — use the equipment endpoint below.

### Wildcard search (list all patients)
```python
r = session.get(f"{BASE}/proxy/patientgateway-v1-server/patient/search/wildcard", params={
    "page": "1", "pageSize": "50", "sortBy": "lastName", "sortOrder": "asc",
    "active": "true", "inactive": "false",
}, headers=headers)
```

### Patient detail
```python
r = session.get(f"{BASE}/proxy/patientgateway-v1-server/patient/{patient_id}", headers=headers)
```

Returns full patient object with demographics, clinicians, locations, compliance info.

## Equipment / Device Serial

```python
r = session.get(f"{BASE}/proxy/equipment-v1-0-server/patient/{patient_id}/equipment",
    headers=headers)
```

Returns array of equipment:
```json
[
  {
    "serialNumber": "D1324454689470",
    "patientUUID": "uuid",
    "equipmentType": "PinnaclePlus",
    "dateAssigned": "2020-02-25T16:43:17.400Z",
    "equipmentId": "uuid",
    "isPrimary": true,
    "datasource": "Modem",
    "deviceSubFamily": "PinnaclePlus"
  }
]
```

Use `isPrimary: true` to select the active device. The `serialNumber` is needed for
report generation.

## Sleep Trend Report Download

### Report templates
```python
r = session.get(f"{BASE}/proxy/therapyreporttemplates-v1-0-server/api/v1/reports/templates",
    headers=headers)
```

Key templates:
| Name | Template ID | Class | Family |
|---|---|---|---|
| Trend (Sleep Trend) | `ebedbf1a-be12-4756-9661-85dc7bec1792` | trend | 1 |
| ComplianceSummary | `e9ff1ef7-bfac-468c-81c2-5d401790254e` | complianceSummary | 1 |
| Detail | `03b714a7-60a2-4971-b464-775554480bbb` | detail | 1 |
| Summary | `b1a1e0fc-30de-485b-bf8c-f61dcdea9fe9` | summary | 1 |
| Patient | `ecd5601b-03c1-4127-b0f6-60bc877ea413` | patient | 1 |

### Report generation

```python
r = session.post(f"{BASE}/proxy/therapyreporttemplates-v1-0-server/api/v1/reports/generate",
    json={
        "templateId": template_id,
        "patientId": patient_id,
        "deviceSerialNumber": serial_number,
        "startDate": "YYYY-MM-DD",
        "endDate": "YYYY-MM-DD",
        "fileName": "sleep-report.pdf",
        "reportComplianceByBlowerTime": False,
        "bestNumberOfDays": 30,
    }, headers=headers)
```

**⚠️ STATUS (March 2026):** Returns 502 Bad Gateway. The `/reports/generate` endpoint
internally depends on `sapphiregateway-v1-server`, which is broken on Philips' side.
This is a server-side issue — cannot be fixed from the client. Until Philips resolves
this, Sleep Trend reports must be pulled manually from careorchestrator.com.

### Presigned S3 URLs (for previously generated reports)
```python
r = session.get(f"{BASE}/proxy/documents-v1-0-server/reports/presigned",
    params={"objectId": patient_id, "documentId": doc_id}, headers=headers)
# Returns: {"presignedUrl": "https://s3.amazonaws.com/..."}
pdf = session.get(presigned_url)
```

Note: The presigned endpoint returns a URL even without a documentId, but the S3 object
will 404 unless a report has already been generated for that patient.

## Config

App config encrypted at `/app/config` with key `1NVo1ERmwN-0i7vK4zL7FcLn_F_qaRL2Ov-KJCTi4TI`.
Uses CryptoJS AES (OpenSSL Salted__ format, EVP_BytesToKey MD5 key derivation).

Decrypted config confirms `sapphiregateway` is the internal gateway:
```json
{"gatewayUrl": "http://sapphireprod-sapphiregateway-v1-server.cloud.pcftest.com:80"}
```

## Session Management

Token `timeToLive` is 10800 seconds (3 hours max). Idle timeout unknown but likely ~20-30 min.

### Pre-flight check
```python
def check_co_session(session, headers):
    r = session.get(f"{BASE}/proxy/patientgateway-v1-server/patient/search/wildcard",
        params={"page":"1","pageSize":"1","sortBy":"lastName","sortOrder":"asc",
                "active":"true","inactive":"false"}, headers=headers)
    return r.status_code == 200
```

### Re-auth (instant, no MFA)
CO re-auth is instant — just re-encrypt password and POST. No user interaction needed.
Re-auth silently if session check fails. Force re-auth every 15 minutes as safety net.

## Working Endpoints Summary

| Endpoint | Status | Notes |
|---|---|---|
| `sapphiregateway-v1-server/authentication/logins` | ✅ | Login only |
| `auth-v2-server/sessions/context` | ✅ | Set org context |
| `patientgateway-v1-server/patient/search` | ✅ | **Use this for search** |
| `patientgateway-v1-server/patient/search/wildcard` | ✅ | List all patients |
| `patientgateway-v1-server/patient/{id}` | ✅ | Patient detail |
| `equipment-v1-0-server/patient/{id}/equipment` | ✅ | Device serial numbers |
| `therapyreporttemplates-v1-0-server/.../templates` | ✅ | List report templates |
| `therapyreporttemplates-v1-0-server/.../generate` | ❌ 502 | Depends on broken sapphiregateway |
| `documents-v1-0-server/reports/presigned` | ✅ | Only if report exists |
| `sapphiregateway-v1-server/patient/search` | ❌ 500 | **Do NOT use** |
