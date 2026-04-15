# React Health Connect Reference

**Report type:** Compliance and Therapy
**Base URL:** `https://portal.reacthealth.com`
**Auth:** Laravel CSRF token, no MFA, no SSO
**Transport:** Python `requests` for auth/search. Headless Chrome for PDF generation.

## Authentication

```python
from bs4 import BeautifulSoup
import re

session = requests.Session()
session.headers.update({"User-Agent": SAFARI_UA})

# Get CSRF token
r = session.get("https://portal.reacthealth.com/verify-login")
soup = BeautifulSoup(r.text, "html.parser")
csrf = soup.find("input", {"name": "_token"})["value"]

# Login
r = session.post("https://portal.reacthealth.com/login", data={
    "_token": csrf,
    "email": EMAIL,
    "password": PASSWORD,
    "remember": "",
    "device_token": "",
}, allow_redirects=True)
# Success redirects to /patients

# Get fresh CSRF for API calls
csrf_meta = re.search(r'<meta[^>]*name="csrf_token"[^>]*content="([^"]+)', r.text)
csrf_token = csrf_meta.group(1) if csrf_meta else csrf
```

## API Headers

All API calls require both headers:
```python
headers = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-CSRF-TOKEN": csrf_token,
}
```

## Patient Search

```python
r = session.get("https://portal.reacthealth.com/api/patients", headers=headers)
patients = r.json()["data"]  # list of patient dicts
```

Patient keys: `id, assigned_id, last_name, first_name, phone, city, state, zip,
current_serial, therapy_start, sex, birth_date, active, last_report, ...`

Also available: `/api/icode-reports` — returns compliance metrics JSON with
`icode`, `serial`, `patient_id`, `compliant_days`, `minutes`, `ahi`, `leak`, etc.

## Compliance and Therapy Report

### Report page (HTML with embedded data)
```
GET /patients/{id}/report/compliance?type=custom&start_date={YYYY-MM-DD}&end_date={YYYY-MM-DD}&show_ep=0
```

Returns a print-ready HTML page containing:
- Inline CSS (comment: "EXTERNAL STYLESHEETS SLOW DOWN domPDF PROCESSING")
- `<vue-compliance-report>` component with ALL data as JSON in `:patient` and `:usage_chart` props
- Hidden form `#pdf_form` that POSTs rendered HTML to `/report` for domPDF conversion

### PDF generation flow (in-browser)
1. Vue renders the report client-side
2. JS clones DOM, converts SVGs to data URIs, strips `.remove-from-pdf` elements
3. Sets `outerHTML` as `pdf_html` form field
4. POSTs to `/report` → domPDF generates PDF

### Getting the PDF programmatically

**Option A: Headless Chrome (recommended)**
Selenium works for this site — no Okta/proxy issues.
1. Login via Selenium or inject `requests` session cookies
2. Navigate to report URL
3. Wait for Vue to render
4. Use Chrome DevTools Protocol print-to-PDF, or trigger the built-in form submission

**Option B: Extract data from Vue props**
The `:patient` and `:usage_chart` props contain all compliance/therapy data as JSON.
Could build a static HTML and POST to `/report` — but exact domPDF format not yet tested.

**⚠️ STATUS:** Auth and patient search work. PDF generation needs headless Chrome implementation.

## Key Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/verify-login` | GET | Get CSRF token |
| `/login` | POST | Authenticate |
| `/api/patients` | GET | List patients (JSON) |
| `/api/icode-reports` | GET | Compliance metrics (JSON) |
| `/patients/{id}/report/compliance` | GET | Report HTML (Vue SPA) |
| `/report` | POST | Convert rendered HTML to PDF |

## Session Management

No MFA — re-auth is instant (GET CSRF → POST login). Re-auth silently if session check fails.

### Pre-flight check
```python
def check_rh_session(session, headers):
    r = session.get("https://portal.reacthealth.com/api/patients", headers=headers)
    return r.status_code == 200
```

## PDF Generation Strategy (in priority order)

**Try Option A first. Only fall back to B if A fails.**

### Option A: Extract data + static HTML → POST to /report (preferred)
The Vue component props contain all compliance/therapy data as JSON. Strategy:
1. GET the report page via `requests`
2. Parse `:patient` and `:usage_chart` JSON from the Vue component attributes
3. Build a static HTML page matching the domPDF format (inline CSS, no external deps)
4. POST to `/report` with `_token` and `pdf_html` fields
5. Response should be the PDF

This avoids browser automation entirely. Risk: domPDF may reject HTML that doesn't
exactly match the Vue-rendered output. Test with one patient first.

### Option B: Selenium via bash_tool (fallback)
If Option A fails, install selenium + chromium in the container:
```bash
pip install selenium --break-system-packages
apt-get install -y chromium-browser 2>/dev/null || true
```
Then: login via Selenium → navigate to report URL → wait for Vue render →
trigger the built-in PDF form submission or use Chrome print-to-PDF.

**This is scripted browser automation via bash_tool, NOT Claude's native
screenshot+click computer use.** The script runs as a Python process.
