#!/usr/bin/env python3
"""
pap-compliance/scripts/diagnose_co.py

When CO "stops connecting" and the skill just prints "❌ Failed after 3 attempts",
run this script. It walks the CO auth+data flow step by step, logs HTTP status +
body snippet at every hop, sweeps the clock offset, and probes the endpoints that
would tell us what Philips changed.

No behaviour from the main pipeline depends on this script — it's a probe.

Usage:
    python scripts/diagnose_co.py

Output:
    /home/claude/co_diag.json  — full capture, safe to share (passwords redacted)

What it checks, in order:
    1. Public reachability of careorchestrator.com.
    2. /app/config endpoint (confirms gateway hostname hasn't moved).
    3. Login at the known path with the current 713s offset.
    4. Clock-offset sweep: ±30s around the current offset if (3) fails.
    5. Alternate login paths (auth-v2-server, sapphiregateway-v2, etc.).
    6. Alternate header shapes (Authorization: Bearer, X-Auth-Token).
    7. Context set + re-login dance.
    8. patientgateway wildcard (proves the session can actually read data).
    9. documents-v1-0-server reachability (the new reports endpoint per
       references/co_reports_api.md).
"""
import sys
import os
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    new_session, load_creds, co_encrypt_password, co_secret_key, CREDS_PATH,
)

CO_BASE = "https://www.careorchestrator.com"
OUT_PATH = "/home/claude/co_diag.json"


def redact(s, keep=4):
    if not s:
        return s
    return s[:keep] + "…(" + str(len(s) - keep) + " chars redacted)"


def snapshot(resp, max_body=500):
    """Compact, JSON-safe summary of an HTTP response."""
    try:
        body = resp.text
    except Exception as e:
        body = f"<read failed: {e}>"
    return {
        "status": resp.status_code,
        "url": resp.url,
        "content_type": resp.headers.get("Content-Type"),
        "content_length": resp.headers.get("Content-Length"),
        "server": resp.headers.get("Server"),
        "set_cookie_names": sorted({c.name for c in resp.cookies}),
        "body_snippet": body[:max_body],
        "body_truncated": len(body) > max_body,
    }


def try_login(session, username, password, url, app_id="Sapphire", offset=None):
    """One login attempt. Returns (ok, auth_dict_or_none, snapshot)."""
    if offset is not None:
        os.environ["CO_CLOCK_OFFSET"] = str(offset)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    body = {
        "username": username,
        "encryptedPassword": co_encrypt_password(password),
        "applicationId": app_id,
        "timeStamp": ts,
    }
    try:
        r = session.post(
            url,
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
    except Exception as e:
        return False, None, {"error": f"transport: {e}", "url": url}
    snap = snapshot(r)
    snap["offset_used"] = offset if offset is not None else int(os.environ.get("CO_CLOCK_OFFSET", "713"))
    snap["app_id_used"] = app_id
    try:
        auth = r.json()
    except Exception:
        return False, None, snap
    ok = r.status_code < 400 and isinstance(auth, dict) and "token" in auth
    if ok:
        snap["token_shape"] = sorted(auth["token"].keys()) if isinstance(auth["token"], dict) else type(auth["token"]).__name__
        snap["userTopOrgId_present"] = bool(auth.get("userTopOrgId"))
    return ok, (auth if ok else None), snap


def probe_get(session, url, headers=None, params=None):
    try:
        r = session.get(url, headers=headers or {}, params=params or {}, timeout=30)
    except Exception as e:
        return {"url": url, "error": f"transport: {e}"}
    return snapshot(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", default=CREDS_PATH)
    ap.add_argument("--offsets", default="713,683,743,653,773,600,800,0",
                    help="Comma-separated clock offsets (seconds) to sweep if the known-good offset fails.")
    args = ap.parse_args()

    report = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": []}

    def log(step, data):
        print(f"\n── {step} ──", flush=True)
        if isinstance(data, dict):
            for k, v in data.items():
                if k == "body_snippet" and isinstance(v, str) and len(v) > 160:
                    v = v[:160] + "…"
                print(f"  {k}: {v}", flush=True)
        else:
            print(f"  {data}", flush=True)
        report["steps"].append({"step": step, "data": data})

    # 1. Creds
    try:
        creds = load_creds(args.creds)
        co = creds["CareOrchestrator"]
        username = co["username"]
        password = co["password"]
        log("1. creds loaded", {"username": redact(username), "password_len": len(password)})
    except Exception as e:
        log("1. creds load FAILED", {"error": str(e)})
        _save(report)
        return 2

    sess = new_session()

    # 2. Reachability (public landing page)
    log("2. reachability /", probe_get(sess, f"{CO_BASE}/"))

    # 3. App config (encrypted blob; confirms static assets still served)
    log("3. /app/config", probe_get(sess, f"{CO_BASE}/app/config"))

    # 4. Primary login at known-good offset
    current_offset = int(os.environ.get("CO_CLOCK_OFFSET", "713"))
    log("4. current offset + derived key",
        {"offset": current_offset, "secret_key": co_secret_key()})
    ok, auth, snap = try_login(
        new_session(), username, password,
        f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins",
        offset=current_offset,
    )
    log("5. login @ sapphiregateway-v1-server (current offset)", snap)

    # 5. Offset sweep — only if (4) failed
    sweep_results = []
    if not ok:
        log("6. sweeping clock offsets", {"offsets": args.offsets})
        for off_str in args.offsets.split(","):
            try:
                off = int(off_str.strip())
            except ValueError:
                continue
            if off == current_offset:
                continue
            sok, sauth, ssnap = try_login(
                new_session(), username, password,
                f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins",
                offset=off,
            )
            sweep_results.append({"offset": off, "ok": sok, "status": ssnap.get("status"),
                                   "snippet": (ssnap.get("body_snippet") or "")[:120]})
            if sok:
                ok, auth, snap = sok, sauth, ssnap
                log(f"   ✅ offset={off} worked — CO may have retuned the clock drift", ssnap)
                break
        log("7. offset sweep results", sweep_results)

    # 6. Alternate login paths — only if still failed
    alt_paths = [
        "/proxy/sapphiregateway-v2-server/authentication/logins",
        "/proxy/auth-v2-server/authentication/logins",
        "/proxy/authentication-v1-0-server/authentication/logins",
        "/api/authentication/logins",
        "/proxy/sapphiregateway-v1-server/authentication/login",  # singular
    ]
    alt_app_ids = ["Sapphire", "CareOrchestrator", "CO", "Sapphire-Web"]
    alt_results = []
    if not ok:
        log("8. probing alternate login paths / applicationIds", None)
        for path in alt_paths:
            for app_id in alt_app_ids:
                aok, aauth, asnap = try_login(
                    new_session(), username, password,
                    f"{CO_BASE}{path}", app_id=app_id, offset=current_offset,
                )
                alt_results.append({"path": path, "app_id": app_id, "ok": aok,
                                    "status": asnap.get("status"),
                                    "snippet": (asnap.get("body_snippet") or "")[:120]})
                if aok:
                    ok, auth, snap = aok, aauth, asnap
                    log(f"   ✅ {path} + applicationId={app_id} worked", asnap)
                    break
            if ok:
                break
        log("9. alternate path/app_id results", alt_results)

    if not ok:
        log("10. login FAILED across all probes", {"final_snapshot": snap})
        _save(report)
        return 3

    # 7. Post-login: build header, hit context, re-login, probe patientgateway.
    token = auth["token"]
    org_id = auth.get("userTopOrgId")
    header_variants = {
        "auth_token-json": {"Accept": "application/json", "Content-Type": "application/json",
                            "auth_token": json.dumps(token)},
        "bearer-hash": {"Accept": "application/json", "Content-Type": "application/json",
                        "Authorization": f"Bearer {token.get('hash','')}" if isinstance(token, dict) else f"Bearer {token}"},
        "x-auth-token-hash": {"Accept": "application/json", "Content-Type": "application/json",
                              "X-Auth-Token": token.get("hash","") if isinstance(token, dict) else str(token)},
    }
    log("11. token shape", {"type": type(token).__name__,
                            "keys": sorted(token.keys()) if isinstance(token, dict) else None,
                            "org_id": org_id})

    context_snap = probe_get(
        sess,  # irrelevant — using POST below
        f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
    )  # GET just to see if the path responds
    log("12. sessions/context GET (expected 404/405)", context_snap)

    # Actually POST context with the primary header variant and observe.
    try:
        r = new_session().post(
            f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
            json={"orgId": org_id},
            headers=header_variants["auth_token-json"],
            timeout=30,
        )
        log("13. sessions/context POST", snapshot(r))
    except Exception as e:
        log("13. sessions/context POST FAILED", {"error": str(e)})

    # 8. patientgateway wildcard under each header variant — the real proof.
    for label, hdrs in header_variants.items():
        s2 = new_session()
        pg_snap = probe_get(
            s2,
            f"{CO_BASE}/proxy/patientgateway-v1-server/patient/search/wildcard",
            headers=hdrs,
            params={"page": "1", "pageSize": "1", "sortBy": "lastName",
                    "sortOrder": "asc", "active": "true", "inactive": "false"},
        )
        log(f"14. patientgateway wildcard — header={label}", pg_snap)

    # 9. documents-v1-0-server reachability — the reports endpoint per co_reports_api.md
    docs_snap = probe_get(
        new_session(),
        f"{CO_BASE}/api/documents-v1-0-server/reports/generate",
        headers=header_variants["auth_token-json"],
    )
    log("15. documents-v1-0-server/reports/generate GET (expect 'Cannot GET' or 405)", docs_snap)

    # 10. Old reports path — did it come back or stay dead?
    old_reports_snap = probe_get(
        new_session(),
        f"{CO_BASE}/proxy/therapyreporttemplates-v1-0-server/api/v1/reports/templates",
        headers=header_variants["auth_token-json"],
    )
    log("16. therapyreporttemplates templates list", old_reports_snap)

    # 11. Live reports probe. Previous run showed ALL body variants return 400 with
    # empty octet-stream body — uniform rejection regardless of body shape means the
    # issue is likely a missing header (XSRF, Origin, Referer, Accept, etc.), not
    # the POST body. This probe focuses on header variants instead.
    from utils import CO_TEMPLATES, CO_REPORT_ROUTES, co_get_equipment_serial
    live_sess = new_session()
    hdrs = header_variants["auth_token-json"]

    # Re-do auth on live_sess so cookies are there
    _co_login_url = f"{CO_BASE}/proxy/sapphiregateway-v1-server/authentication/logins"
    ok2, auth2, login_snap = try_login(live_sess, username, password, _co_login_url,
                                       offset=snap.get("offset_used", 713))
    if not ok2:
        log("17. re-login for reports probe FAILED", {"snapshot": login_snap})
        _save(report)
        print(f"\nFull capture saved to {OUT_PATH}.", flush=True)
        return 0

    # Context set + re-login (the full auth dance)
    live_hdrs = {"Accept": "application/json", "Content-Type": "application/json",
                 "auth_token": json.dumps(auth2.get("token", {}))}
    live_sess.post(f"{CO_BASE}/proxy/auth-v2-server/sessions/context",
                   json={"orgId": auth2.get("userTopOrgId")}, headers=live_hdrs, timeout=30)
    ok3, auth3, _ = try_login(live_sess, username, password, _co_login_url,
                              offset=snap.get("offset_used", 713))
    if ok3:
        live_hdrs = {"Accept": "application/json", "Content-Type": "application/json",
                     "auth_token": json.dumps(auth3.get("token", {}))}

    # 17. Dump ALL cookies — look for XSRF-TOKEN or similar
    cookie_dump = {c.name: c.value for c in live_sess.cookies}
    log("17. session cookies after full auth dance", cookie_dump)

    xsrf_cookie = None
    for name in ["XSRF-TOKEN", "xsrf-token", "CSRF-TOKEN", "csrf-token",
                 "_csrf", "csrftoken", "JSESSIONID"]:
        if name in cookie_dump:
            xsrf_cookie = (name, cookie_dump[name])
            break
    if not xsrf_cookie:
        for name, val in cookie_dump.items():
            if "csrf" in name.lower() or "xsrf" in name.lower():
                xsrf_cookie = (name, val)
                break
    log("17a. XSRF cookie", {"found": bool(xsrf_cookie),
                              "name": xsrf_cookie[0] if xsrf_cookie else None,
                              "value_len": len(xsrf_cookie[1]) if xsrf_cookie else 0})

    # 18. Find a patient WITH a serial number (search more patients)
    wc = live_sess.get(
        f"{CO_BASE}/proxy/patientgateway-v1-server/patient/search/wildcard",
        params={"page": "1", "pageSize": "50", "sortBy": "lastName",
                "sortOrder": "asc", "active": "true", "inactive": "false"},
        headers=live_hdrs, timeout=30)
    try:
        pts = wc.json() if wc.status_code == 200 else []
    except Exception:
        pts = []
    if not pts or not isinstance(pts, list):
        log("18. no patients returned", {"status": wc.status_code})
        _save(report)
        return 0

    log("18. patients fetched", {"count": len(pts)})

    # Find first patient with a serial
    probe_uuid = None
    serial = None
    probe_name = None
    for pt in pts:
        pid = pt.get("patientId")
        s, eq = co_get_equipment_serial(live_sess, live_hdrs, pid)
        if s:
            probe_uuid = pid
            serial = s
            probe_name = f"{pt.get('lastName','')}, {pt.get('firstName','')}"
            log("18a. patient WITH serial found", {"uuid": probe_uuid,
                                                    "name": probe_name,
                                                    "serial": serial})
            break

    if not probe_uuid:
        # Fall back to first patient, no serial
        probe_uuid = pts[0].get("patientId")
        probe_name = f"{pts[0].get('lastName','')}, {pts[0].get('firstName','')}"
        log("18a. no patient with serial in first 50 — using first patient",
            {"uuid": probe_uuid, "name": probe_name})

    # 19. Header variant sweep on the generate endpoint.
    # Use a single body shape (minimal: templateId + patientId + dates + serial if avail).
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=30)
    gen_body = {
        "templateId": CO_TEMPLATES["sleep_trend"],
        "patientId": probe_uuid,
        "startDate": start_dt.strftime("%Y-%m-%d"),
        "endDate": end_dt.strftime("%Y-%m-%d"),
    }
    if serial:
        gen_body["deviceSerialNumber"] = serial

    route = CO_REPORT_ROUTES[0]
    gen_url = f"{CO_BASE}{route}"

    # Build header variants to try
    hdr_variants = []

    # A: current (known 400)
    hdr_variants.append(("baseline", {**live_hdrs}))

    # B: + X-Requested-With (Angular default for AJAX)
    hdr_variants.append(("+ x-requested-with", {**live_hdrs,
                         "X-Requested-With": "XMLHttpRequest"}))

    # C: + Origin + Referer
    hdr_variants.append(("+ origin+referer", {**live_hdrs,
                         "Origin": "https://www.careorchestrator.com",
                         "Referer": "https://www.careorchestrator.com/"}))

    # D: + Accept: application/octet-stream (match expected response type)
    hdr_variants.append(("+ accept-octet", {**live_hdrs,
                         "Accept": "application/octet-stream, application/json"}))

    # E: + XSRF header (if cookie found)
    if xsrf_cookie:
        for hdr_name in ["X-XSRF-TOKEN", "X-CSRF-TOKEN"]:
            hdr_variants.append((f"+ {hdr_name}", {**live_hdrs,
                                 hdr_name: xsrf_cookie[1]}))

    # F: kitchen sink — all of the above combined
    kitchen = {**live_hdrs,
               "X-Requested-With": "XMLHttpRequest",
               "Origin": "https://www.careorchestrator.com",
               "Referer": "https://www.careorchestrator.com/",
               "Accept": "application/octet-stream, application/json"}
    if xsrf_cookie:
        kitchen["X-XSRF-TOKEN"] = xsrf_cookie[1]
        kitchen["X-CSRF-TOKEN"] = xsrf_cookie[1]
    hdr_variants.append(("kitchen_sink", kitchen))

    # G: Content-Type: application/octet-stream instead of JSON (maybe it wants binary?)
    hdr_variants.append(("content-type-octet", {**live_hdrs,
                         "Content-Type": "application/octet-stream"}))

    log("19. body used for all header variants", gen_body)

    for idx, (hdr_label, test_hdrs) in enumerate(hdr_variants, start=1):
        try:
            gr = live_sess.post(gen_url, json=gen_body, headers=test_hdrs, timeout=60)
            gen_snap = {
                "headers_label": hdr_label,
                "extra_headers": {k: v for k, v in test_hdrs.items()
                                  if k not in live_hdrs or test_hdrs[k] != live_hdrs.get(k)},
                "status": gr.status_code,
                "content_type": gr.headers.get("Content-Type"),
                "content_length": gr.headers.get("Content-Length"),
                "body_snippet": gr.text[:500] if gr.content[:5] != b"%PDF-"
                                else f"<PDF {len(gr.content)} bytes>",
                "body_hex_head": gr.content[:20].hex() if gr.content else "",
                "is_pdf": gr.content[:5] == b"%PDF-",
                "response_headers": {k: v for k, v in gr.headers.items()
                                     if k.lower() in ("content-type", "content-length",
                                                      "x-request-id", "set-cookie",
                                                      "www-authenticate", "x-xsrf-token")},
            }
            if gr.status_code == 200 and len(gr.content) > 0:
                log(f"20.{idx} [{hdr_label}] GOT CONTENT!", gen_snap)
                if gr.content[:5] == b"%PDF-":
                    log(f"  PDF received — {hdr_label} is the winning combo", None)
                break
            if gr.status_code != 400:
                log(f"20.{idx} [{hdr_label}] DIFFERENT STATUS — {gr.status_code}", gen_snap)
            else:
                log(f"20.{idx} [{hdr_label}] 400 (same as before)", gen_snap)
        except Exception as e:
            log(f"20.{idx} [{hdr_label}] EXCEPTION",
                {"error": f"{type(e).__name__}: {e}"})

    _save(report)
    print(f"\nFull capture saved to {OUT_PATH} — share that file (no secrets).", flush=True)
    return 0


def _save(report):
    report["ended_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(OUT_PATH, "w") as f:
            json.dump(report, f, indent=2, default=str)
    except Exception as e:
        print(f"\n⚠️  Could not write {OUT_PATH}: {e}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
