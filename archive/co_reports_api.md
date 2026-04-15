# AirView (ResMed) Reference

**Report type:** Compliance and Therapy
**Base URL:** `https://airview.resmed.com`
**Auth:** Okta SSO at `https://airviewid.resmed.com` with email MFA
**Transport:** Python `requests` ONLY — Selenium fails behind egress proxy

Safari User-Agent required on ALL requests:
```
Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15
```

## Authentication

### Step 1: Primary auth
```python
r = session.post("https://airviewid.resmed.com/api/v1/authn", json={
    "username": USERNAME, "password": PASSWORD,
    "options": {"multiOptionalFactorEnroll": True, "warnBeforePasswordExpired": True}
}, headers={"Accept": "application/json", "Content-Type": "application/json"})
auth = r.json()  # status: "MFA_REQUIRED"
state_token = auth["stateToken"]
```

### Step 2: Trigger email MFA
```python
factor = [f for f in auth["_embedded"]["factors"] if f["factorType"] == "email"][0]
verify_url = factor["_links"]["verify"]["href"]
r = session.post(verify_url, json={"stateToken": state_token},
    headers={"Accept": "application/json", "Content-Type": "application/json"})
```
Save `stateToken` and `verify_url` — do NOT re-trigger MFA. Ask user for code.

### Step 3: Verify MFA code
```python
r = session.post(verify_url, json={"stateToken": state_token, "passCode": USER_CODE},
    headers={"Accept": "application/json", "Content-Type": "application/json"})
# status: "SUCCESS", get sessionToken
session_token = r.json()["sessionToken"]
```

### Step 4: OAuth exchange
```python
# Get state/nonce from AirView login redirect
r = session.get("https://airview.resmed.com/", allow_redirects=True)
# Extract state and nonce from URL params or page JS

r = session.get("https://airviewid.resmed.com/oauth2/aus7x84n01F9ecUUX297/v1/authorize", params={
    "client_id": "0oa7ca7b9yNqH8sBI297", "response_type": "code",
    "scope": "openid email profile offline_access",
    "redirect_uri": "https://login.airview.resmed.com/authorization-code/callback",
    "state": real_state, "nonce": real_nonce, "sessionToken": session_token,
}, allow_redirects=False)
# Follow all redirects manually until no more Location headers
```

### Step 5: Verify session
```python
r = session.get("https://airview.resmed.com/patients", params={"q": "test", "selectedStatus": "Active"})
assert "sign-in-widget" not in r.text  # Session is live
```

Save session cookies with `pickle` for reuse.

## Patient Search

```python
r = session.get("https://airview.resmed.com/patients", params={
    "q": "Last First", "search": "Search patients", "selectedStatus": "All"
})
```

Parse HTML table. Each `<tr class="patient-row">` has:
- `ecn` attribute = patient UUID
- Cells: Type | Name | (icon) | Available data | Compliant | Last 30 | Last updated

### Multi-profile handling
1. Match both last AND first name (case-insensitive)
2. If no match, retry with last name only
3. **STRICT MATCH RULE:** If the AirView result has a different first name, DO NOT download.
   That's a different patient — likely on a different manufacturer (Philips/React Health).
   Only download when both first AND last name match. Examples of bad matches to reject:
   - Schedule says "Floyd, Garry" → AirView returns "Fields, Floyd" (Floyd matched as first name)
   - Schedule says "Burford, Arianna" → AirView returns "BURFORD, XAVIER" (wrong first name)
   - Schedule says "Harvey, Amber" → AirView returns "Brown, Harvey" (Harvey matched as first name)
4. **VERIFY DOB BEFORE DOWNLOADING.** After finding a name match, open the patient profile
   and confirm DOB matches before pulling any report. Name matches alone have produced
   wrong-patient downloads. If the schedule doesn't have DOB, compare age from the
   schedule's Age/Gender column against the profile's DOB. If DOB can't be verified,
   flag the patient for user review — do NOT download blindly.
5. Sort ALL matches by "Last updated" recency
   `Today > Yesterday > X days > X weeks > X months > Over 1 year > --`
4. Try each profile in order when downloading

### Recency scoring
```python
def recency_score(updated_text):
    s = updated_text.lower().strip()
    if s == "today": return 10000
    if s == "yesterday": return 9999
    m = re.search(r'(\d+)\s*day', s)
    if m: return 10000 - int(m.group(1))
    m = re.search(r'(\d+)\s*month', s)
    if m: return 10000 - int(m.group(1)) * 30
    if "year" in s: return 100
    return 0
```

## Report Download

### Compliance and Therapy report
```python
timestamp = datetime.now().strftime("%m%d%y_%H%M%S")
url = f"https://airview.resmed.com/patients/{uuid}/report/compliance/Compliance_report_{timestamp}.pdf"
```

**⚠️ ALWAYS use SUPPLIED. NEVER use COMPLIANT.**
`reportPeriodType=COMPLIANT` pulls the initial compliance window from device setup, which
could be months or years old. Always use SUPPLIED with an explicit period ending today.

```python
params = {
    "returningUrl": "/patients", "reportType": "Compliance",
    "reportPeriodType": "SUPPLIED",
    "reportingPeriodLength": "30",  # or "90"
    "reportingPeriodEnd": "MM/DD/YYYY"  # today's date
}
```

Note: SUPPLIED 30-day can sometimes return empty even when 90-day works. If 30-day fails,
still pull the 90-day.

Success = `Content-Type: application/pdf` with bytes starting `%PDF`.

### Filename conventions
- 30-day: `{AirViewName}.pdf` (e.g., `WARD, LESHIKA.pdf`)
- 90-day: `{AirViewName} 90.pdf` (e.g., `WARD, LESHIKA 90.pdf`)

Use the name **as it appears in AirView** — preserves correct spelling.

### Rate limiting
`time.sleep(0.8)` between patient requests.

## Session Management

AirView sessions expire after ~20-30 minutes (observed, not documented).

### Pre-flight check before each patient
```python
def check_airview_session(session):
    r = session.get("https://airview.resmed.com/patients",
        params={"q": "test", "selectedStatus": "Active"})
    return "sign-in-widget" not in r.text

# In batch loop:
if not check_airview_session(session):
    print("Session expired — need new MFA code")
    # Re-trigger full auth flow, ask user for new code
```

### Proactive re-auth timer
Force re-auth every 15 minutes regardless of session state:
```python
import time
auth_time = time.time()

# Before each patient:
if time.time() - auth_time > 900:  # 15 min
    # Re-auth even if session looks alive
    auth_time = time.time()
```

AirView re-auth requires a new MFA code — use AskUserQuestion.
