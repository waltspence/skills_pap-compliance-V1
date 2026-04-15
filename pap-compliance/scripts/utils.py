"""
pap-compliance/scripts/utils.py
Shared utilities for the PAP compliance workflow.
"""

import os
import re
import json
import time
import base64
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
CREDS_PATH = "/home/claude/pap_creds.json"
SESSION_DIR = "/home/claude"
REPORTS_DIR = "/home/claude/reports"
OUTPUTS_DIR = "/mnt/user-data/outputs"


# ═══════════════════════════════════════════════════
# Credentials
# ═══════════════════════════════════════════════════

def load_creds(path=CREDS_PATH):
    with open(path) as f:
        return json.load(f)


# ═══════════════════════════════════════════════════
# Name Matching
# ═══════════════════════════════════════════════════

def name_match(search_first, search_last, result_first, result_last):
    """Strict match: both first AND last name must match (partial OK for compounds)."""
    sf = search_first.lower().strip().split()[0]
    sl = search_last.lower().strip().split()[0]
    rf = result_first.lower().strip()
    rl = result_last.lower().strip()
    return (sl in rl or rl in sl) and (sf in rf or rf in sf)


# ═══════════════════════════════════════════════════
# DOB Matching
# ═══════════════════════════════════════════════════

def normalize_dob(dob_str):
    """Parse DOB string into a date object. Handles MM/DD/YYYY, YYYY-MM-DD, MM-DD-YYYY."""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(dob_str.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def dob_matches(profile_dob_str, schedule_dob_str):
    """Compare two DOB strings, return True if they represent the same date."""
    pd = normalize_dob(profile_dob_str)
    sd = normalize_dob(schedule_dob_str)
    if pd and sd:
        return pd == sd
    return False


def extract_dob_from_profile(session, ecn):
    """Fetch AirView patient profile and extract DOB."""
    try:
        r = session.get(f"https://airview.resmed.com/patients/{ecn}", timeout=30)
        if "sign-in-widget" in r.text:
            return None  # session expired

        # Try JSON embedded data
        m = re.search(r'"dateOfBirth"\s*:\s*"([^"]+)"', r.text)
        if m:
            return m.group(1)

        # Try visible text patterns
        m = re.search(r'(?:Date of Birth|DOB)[:\s]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', r.text, re.IGNORECASE)
        if m:
            return m.group(1)

        # Try bare date in stripped strings
        soup = BeautifulSoup(r.text, "html.parser")
        for text in soup.stripped_strings:
            if re.match(r'\d{1,2}/\d{1,2}/\d{4}$', text.strip()):
                return text.strip()

        return "NOT_FOUND"
    except Exception as e:
        return f"ERROR:{e}"


# ═══════════════════════════════════════════════════
# Recency Scoring (AirView "Last updated" text)
# ═══════════════════════════════════════════════════

def recency_score(updated_text):
    s = updated_text.lower().strip()
    if s == "today":
        return 10000
    if s == "yesterday":
        return 9999
    m = re.search(r'(\d+)\s*day', s)
    if m:
        return 10000 - int(m.group(1))
    m = re.search(r'(\d+)\s*week', s)
    if m:
        return 10000 - int(m.group(1)) * 7
    m = re.search(r'(\d+)\s*month', s)
    if m:
        return 10000 - int(m.group(1)) * 30
    if "year" in s or "over" in s:
        return 100
    if s == "--":
        return 0
    return 50


def is_stale(updated_text):
    """Data older than 3 months."""
    s = updated_text.lower().strip()
    if s in ("--", "over 1 year"):
        return True
    if "year" in s:
        return True
    m = re.search(r'(\d+)\s*month', s)
    if m and int(m.group(1)) > 3:
        return True
    return False


def is_over_year(updated_text):
    s = updated_text.lower().strip()
    if "year" in s or s == "--":
        return True
    m = re.search(r'(\d+)\s*month', s)
    if m and int(m.group(1)) >= 12:
        return True
    return False


def parse_avail_days(avail_text):
    """Convert AirView availability text to approximate days."""
    s = avail_text.lower().strip()
    total = 0
    ym = re.search(r'(\d+)\s*year', s)
    if ym:
        total += int(ym.group(1)) * 365
    mm = re.search(r'(\d+)\s*month', s)
    if mm:
        total += int(mm.group(1)) * 30
    dm = re.search(r'(\d+)\s*day', s)
    if dm:
        total += int(dm.group(1))
    if total == 0:
        dm2 = re.match(r'(\d+)', s)
        if dm2:
            total = int(dm2.group(1))
    return total


# ═══════════════════════════════════════════════════
# CO Encryption
# ═══════════════════════════════════════════════════

def co_secret_key():
    """
    CO's JS client derives the AES key from the current UTC time minus ~713 seconds,
    truncated to the ISO string slice [5:21] (format: MM-DDTHH:MM:SS.mmm → 16 chars).
    The 713s offset was reverse-engineered from CO's bundled JS; it compensates for a
    server-side clock skew. If CO ever retunes this, logins will fail en masse with no
    useful error — set env var CO_CLOCK_OFFSET to a new integer (seconds) to override
    without touching code. The hardcoded 713 is the current known-good default.
    """
    from Crypto.Cipher import AES
    offset = int(os.environ.get("CO_CLOCK_OFFSET", "713"))
    now = datetime.now(timezone.utc)
    adjusted = now - timedelta(seconds=offset)
    iso = adjusted.strftime('%Y-%m-%dT%H:%M:%S.') + f"{adjusted.microsecond // 1000:03d}Z"
    return iso[5:21]


def co_encrypt_password(password):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    k = co_secret_key().encode('utf-8')
    return base64.b64encode(
        AES.new(k, AES.MODE_CBC, k).encrypt(pad(password.encode('utf-8'), 16))
    ).decode()


# ═══════════════════════════════════════════════════
# Session Helpers
# ═══════════════════════════════════════════════════

def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": SAFARI_UA})
    return s


def check_av_session(session):
    try:
        r = session.get("https://airview.resmed.com/patients",
                        params={"q": "test", "selectedStatus": "Active"}, timeout=30)
        return "sign-in-widget" not in r.text and r.status_code == 200
    except Exception:
        return False


def check_co_session(session, headers):
    try:
        r = session.get(
            "https://www.careorchestrator.com/proxy/patientgateway-v1-server/patient/search/wildcard",
            params={"page": "1", "pageSize": "1", "sortBy": "lastName",
                    "sortOrder": "asc", "active": "true", "inactive": "false"},
            headers=headers, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


def check_rh_session(session, headers):
    try:
        r = session.get("https://portal.reacthealth.com/api/patients",
                        headers=headers, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════
# Auth Functions (importable by search/download scripts)
# ═══════════════════════════════════════════════════

def auth_co_rh(creds, session_dir=SESSION_DIR):
    """
    Authenticate CO and RH. Saves session pkls.
    Returns dict: {"CO": "✅ ...", "RH": "✅ ..."}
    """
    import pickle
    from bs4 import BeautifulSoup as _BS

    results = {}
    CO_BASE = "https://www.careorchestrator.com"
    CO_MAX_RETRIES = 3
    CO_RETRY_DELAY = 3

    def _co_login(session, username, password):
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        url = f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins"
        r = session.post(url, json={
            "username": username,
            "encryptedPassword": co_encrypt_password(password),
            "applicationId": "Sapphire",
            "timeStamp": ts,
        }, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)
        # Surface HTTP-level failures with status + body snippet. The old code called
        # r.json() unconditionally, so a 4xx/5xx non-JSON body surfaced as an unhelpful
        # JSONDecodeError. If CO migrates the login path or adds an anti-bot check, we
        # need the status/body to tell — blind retries with a fresh AES key won't help.
        if r.status_code >= 400:
            raise ValueError(f"HTTP {r.status_code} at {url} — body[:300]={r.text[:300]!r}")
        try:
            auth = r.json()
        except Exception:
            raise ValueError(f"Non-JSON {r.status_code} at {url} — body[:300]={r.text[:300]!r}")
        if "token" not in auth:
            raise ValueError(f"No token (HTTP {r.status_code}): {json.dumps(auth)[:300]}")
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "auth_token": json.dumps(auth["token"])}
        return auth, headers, auth.get("userTopOrgId")

    # CO
    # Exception types that indicate a coding/config bug, not a transient backend
    # failure. Retrying these is pure waste — they cannot succeed on attempt 2 or 3.
    # Keeping the original exception type in the message also makes the bug class
    # obvious instead of hiding behind a generic "Failed after 3 attempts" wrapper.
    NON_RETRYABLE = (NameError, AttributeError, TypeError, KeyError, ImportError)

    try:
        last_error = None
        attempts_made = 0
        for attempt in range(1, CO_MAX_RETRIES + 1):
            attempts_made = attempt
            try:
                co_session = new_session()
                co_auth, co_headers, co_org_id = _co_login(
                    co_session, creds["CareOrchestrator"]["username"], creds["CareOrchestrator"]["password"])
                co_session.post(f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
                                json={"orgId": co_org_id}, headers=co_headers, timeout=30)
                co_auth2, co_headers2, _ = _co_login(
                    co_session, creds["CareOrchestrator"]["username"], creds["CareOrchestrator"]["password"])
                import os as _os
                with open(_os.path.join(session_dir, "co_session.pkl"), "wb") as f:
                    pickle.dump({"session": co_session, "headers": co_headers2,
                                 "token": co_auth2["token"], "org_id": co_org_id,
                                 "auth_time": time.time()}, f)
                note = f" (attempt {attempt})" if attempt > 1 else ""
                results["CO"] = f"✅ Authenticated{note}"
                last_error = None
                break
            except NON_RETRYABLE as e:
                # Client-side bug — fail fast, surface the type.
                last_error = f"{type(e).__name__}: {e} (client-side bug, not retried)"
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < CO_MAX_RETRIES:
                    time.sleep(CO_RETRY_DELAY)
        if last_error:
            n = "attempt" if attempts_made == 1 else "attempts"
            results["CO"] = f"❌ Failed after {attempts_made} {n}: {last_error}"
    except Exception as e:
        results["CO"] = f"❌ {type(e).__name__}: {e}"

    # RH
    try:
        import re as _re
        rh_session = new_session()
        r = rh_session.get("https://portal.reacthealth.com/verify-login", timeout=30)
        soup = _BS(r.text, "html.parser")
        csrf_input = soup.find("input", {"name": "_token"})
        if not csrf_input:
            results["RH"] = f"❌ No CSRF token (status={r.status_code})"
        else:
            csrf = csrf_input["value"]
            r = rh_session.post("https://portal.reacthealth.com/login", data={
                "_token": csrf, "email": creds["ReactHealth"]["email"],
                "password": creds["ReactHealth"]["password"],
                "remember": "", "device_token": "",
            }, allow_redirects=True, timeout=30)
            csrf_meta = _re.search(r'<meta[^>]*name="csrf_token"[^>]*content="([^"]+)', r.text)
            rh_csrf = csrf_meta.group(1) if csrf_meta else csrf
            rh_headers = {"Accept": "application/json",
                          "X-Requested-With": "XMLHttpRequest",
                          "X-CSRF-TOKEN": rh_csrf}
            test = rh_session.get("https://portal.reacthealth.com/api/patients",
                                  headers=rh_headers, timeout=30)
            if test.status_code == 200:
                rh_patients = test.json().get("data", [])
                if len(rh_patients) == 25:
                    print("  ⚠️ RH cache is exactly 25 — likely truncated by server-side pagination. "
                          "Patients beyond page 1 will silently fail search_rh(). "
                          "Capture /api/patients response shape and add pagination loop.", flush=True)
                import os as _os
                with open(_os.path.join(session_dir, "rh_session.pkl"), "wb") as f:
                    pickle.dump({"session": rh_session, "headers": rh_headers,
                                 "csrf": rh_csrf, "patients": rh_patients,
                                 "auth_time": time.time()}, f)
                results["RH"] = f"✅ Authenticated ({len(rh_patients)} patients cached)"
            else:
                results["RH"] = f"⚠️ Login OK but API returned {test.status_code}"
    except Exception as e:
        results["RH"] = f"❌ {e}"

    return results


def auth_av_trigger(creds, session_dir=SESSION_DIR):
    """
    Capture AV state/nonce, primary auth, trigger MFA email.
    Saves av_pending.pkl. Does NOT call .json() on the trigger response.
    Returns (av_session, state_msg) on success, raises on failure.
    """
    import pickle, re as _re, os as _os
    from urllib.parse import urlparse, parse_qs

    av_session = new_session()

    # Capture state/nonce from redirect
    r = av_session.get("https://airview.resmed.com/", allow_redirects=True, timeout=30)
    parsed = urlparse(r.url)
    params = parse_qs(parsed.query)
    real_state = params.get("state", [None])[0]
    real_nonce = params.get("nonce", [None])[0]
    if not real_state:
        m = _re.search(r'[?&]state=([^&"\']+)', r.url)
        if m: real_state = m.group(1)
        m = _re.search(r'[?&]nonce=([^&"\']+)', r.url)
        if m: real_nonce = m.group(1)

    state_msg = f"state captured ({real_state[:20]}...)" if real_state else "⚠️ state NOT captured"

    # Primary auth
    r2 = av_session.post("https://airviewid.resmed.com/api/v1/authn", json={
        "username": creds["AirView"]["email"],
        "password": creds["AirView"]["password"],
        "options": {"multiOptionalFactorEnroll": True, "warnBeforePasswordExpired": True}
    }, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=30)

    auth = r2.json()
    if auth.get("status") != "MFA_REQUIRED":
        if auth.get("status") == "SUCCESS":
            # No MFA needed — complete OAuth directly
            session_token = auth["sessionToken"]
            av_session.get("https://airviewid.resmed.com/oauth2/aus7x84n01F9ecUUX297/v1/authorize",
                           params={"client_id": "0oa7ca7b9yNqH8sBI297", "response_type": "code",
                                   "scope": "openid email profile offline_access",
                                   "redirect_uri": "https://login.airview.resmed.com/authorization-code/callback",
                                   "state": real_state or "fallback", "nonce": real_nonce or "fallback",
                                   "sessionToken": session_token},
                           allow_redirects=True, timeout=30)
            with open(_os.path.join(session_dir, "av_session.pkl"), "wb") as f:
                pickle.dump({"session": av_session, "auth_time": time.time()}, f)
            return av_session, "✅ No MFA needed — session live"
        raise ValueError(f"Unexpected AV auth status: {auth.get('status')}")

    state_token = auth["stateToken"]
    email_factor = [f for f in auth["_embedded"]["factors"] if f["factorType"] == "email"][0]
    verify_url = email_factor["_links"]["verify"]["href"]

    # Trigger MFA — do NOT call .json() on this response
    r3 = av_session.post(verify_url, json={"stateToken": state_token},
                         headers={"Accept": "application/json", "Content-Type": "application/json"},
                         timeout=30)
    if r3.status_code != 200:
        raise ValueError(f"MFA trigger failed: HTTP {r3.status_code} — {r3.text[:200]}")

    with open(_os.path.join(session_dir, "av_pending.pkl"), "wb") as f:
        pickle.dump({"session": av_session, "state_token": state_token,
                     "verify_url": verify_url, "real_state": real_state,
                     "real_nonce": real_nonce, "auth_time": time.time()}, f)

    return av_session, state_msg


def auth_av_verify(code, session_dir=SESSION_DIR):
    """
    Verify AirView MFA code and complete OAuth.
    Saves av_session.pkl. Returns True on success, raises on failure.
    """
    import pickle, os as _os

    pending_path = _os.path.join(session_dir, "av_pending.pkl")
    if not _os.path.exists(pending_path):
        raise FileNotFoundError("No pending AV auth. Run auth_av_trigger() first.")

    with open(pending_path, "rb") as f:
        pending = pickle.load(f)

    av_session = pending["session"]
    state_token = pending["state_token"]
    verify_url = pending["verify_url"]
    real_state = pending.get("real_state") or "fallback"
    real_nonce = pending.get("real_nonce") or "fallback"

    r = av_session.post(verify_url, json={"stateToken": state_token, "passCode": code},
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                        timeout=30)
    auth = r.json()
    if auth.get("status") != "SUCCESS":
        raise ValueError(f"MFA verify failed: {auth.get('status')} — {str(auth)[:200]}")

    session_token = auth["sessionToken"]
    av_session.get("https://airviewid.resmed.com/oauth2/aus7x84n01F9ecUUX297/v1/authorize",
                   params={"client_id": "0oa7ca7b9yNqH8sBI297", "response_type": "code",
                           "scope": "openid email profile offline_access",
                           "redirect_uri": "https://login.airview.resmed.com/authorization-code/callback",
                           "state": real_state, "nonce": real_nonce,
                           "sessionToken": session_token},
                   allow_redirects=True, timeout=30)

    if not check_av_session(av_session):
        raise ValueError("Session not live after OAuth — state/nonce mismatch?")

    with open(_os.path.join(session_dir, "av_session.pkl"), "wb") as f:
        pickle.dump({"session": av_session, "auth_time": time.time()}, f)

    return True
