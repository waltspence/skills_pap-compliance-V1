#!/usr/bin/env python3
"""
pap-compliance/scripts/search_all.py

Search AirView, Care Orchestrator, and React Health for all queued patients.
DOB verification happens inline during search (not at download time).

Usage:
    python scripts/search_all.py [--queue /path/to/patient_queue.json] [--output /path/to/results.json]
"""

import sys
import os
import json
import pickle
import time
import re
import argparse

sys.path.insert(0, os.path.dirname(__file__))
from utils import (
    name_match, dob_matches, extract_dob_and_serial_from_profile, recency_score,
    is_stale, is_over_year, parse_avail_days, check_av_session,
    co_get_equipment_serial, SESSION_DIR, new_session
)
from bs4 import BeautifulSoup

CO_BASE = "https://www.careorchestrator.com"


def load_session(name):
    path = os.path.join(SESSION_DIR, f"{name}.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


# ═══════════════════════════════════════════════════
# AirView Search (with DOB verify)
# ═══════════════════════════════════════════════════

def search_airview(av_session, last, first, schedule_dob):
    """Search AirView, verify DOB inline. Returns list of verified profiles."""
    query = f"{last} {first}"
    r = av_session.get("https://airview.resmed.com/patients",
                       params={"q": query, "search": "Search patients", "selectedStatus": "All"},
                       timeout=30)

    if "sign-in-widget" in r.text:
        return None  # session expired

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.find_all("tr", class_="patient-row")

    profiles = []
    for row in rows:
        ecn = row.get("ecn", "")
        cells = row.find_all("td")
        if len(cells) < 7:
            continue

        pname = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        available = cells[3].get_text(strip=True) if len(cells) > 3 else ""
        last30 = cells[5].get_text(strip=True) if len(cells) > 5 else ""
        updated = cells[6].get_text(strip=True) if len(cells) > 6 else ""

        parts = pname.split(",", 1)
        rlast = parts[0].strip() if parts else pname
        rfirst = parts[1].strip() if len(parts) == 2 else ""

        if not name_match(first, last, rfirst, rlast):
            continue

        profiles.append({
            "ecn": ecn, "name": pname, "available": available,
            "last30": last30, "updated": updated,
            "recency": recency_score(updated), "portal": "AV",
            "dob_verified": False, "profile_dob": "",
        })

    # Sort by recency, then verify DOB on best non-stale profiles
    profiles.sort(key=lambda x: x["recency"], reverse=True)

    verified = []
    for p in profiles:
        # Skip stale/zero for DOB check (waste of time)
        if is_stale(p["updated"]) and not verified:
            p["skip_reason"] = "stale"
            verified.append(p)  # keep for reporting but don't verify
            continue
        if p["last30"] == "0%":
            p["skip_reason"] = "zero_usage"
            verified.append(p)
            continue

        profile_dob, profile_serial = extract_dob_and_serial_from_profile(av_session, p["ecn"])
        p["profile_dob"] = profile_dob or ""
        p["serial"] = profile_serial or ""
        time.sleep(0.3)

        if profile_dob and dob_matches(profile_dob, schedule_dob):
            p["dob_verified"] = True
            verified.append(p)
            break  # found the right one, stop checking others
        elif profile_dob and profile_dob not in ("NOT_FOUND", ""):
            p["skip_reason"] = f"dob_mismatch:{profile_dob}"
            # Don't add — wrong patient, try next profile
            continue
        else:
            p["skip_reason"] = "dob_unverifiable"
            verified.append(p)

    return verified


# ═══════════════════════════════════════════════════
# Care Orchestrator Search (DOB in search result)
# ═══════════════════════════════════════════════════

def search_co(co_session, co_headers, last, first, schedule_dob):
    """Search CO via patientgateway. DOB comes from search results directly."""
    search_key = last.split()[0]
    try:
        r = co_session.get(f"{CO_BASE}/proxy/patientgateway-v1-server/patient/search",
                           params={"s": search_key, "searchBy": "name", "page": "1",
                                   "pageSize": "50", "sortBy": "lastName", "sortOrder": "asc",
                                   "active": "true", "inactive": "false"},
                           headers=co_headers, timeout=30)
        if r.status_code != 200:
            return {"status": f"error_{r.status_code}", "matches": []}

        results = r.json()
        matches = []
        for p in results:
            pfirst = p.get("firstName", "")
            plast = p.get("lastName", "")
            pdob = p.get("dateOfBirth", "")
            pid = p.get("patientId", "")

            if not name_match(first, last, pfirst, plast):
                continue

            dob_ok = dob_matches(pdob, schedule_dob) if pdob else False
            matches.append({
                "patientId": pid,
                "name": f"{plast}, {pfirst}",
                "dob": pdob,
                "dob_verified": dob_ok,
                "org": p.get("organization", {}).get("orgName", ""),
                "usage_pct": p.get("usagePercentage"),
                "avg_hours": p.get("averageHoursUsed"),
                "portal": "CO",
            })

        verified = [m for m in matches if m["dob_verified"]]
        for m in verified:
            try:
                serial, _ = co_get_equipment_serial(co_session, co_headers, m["patientId"])
                m["serial"] = serial or ""
            except Exception:
                m["serial"] = ""
        return {"status": "ok", "matches": verified}
    except Exception as e:
        return {"status": f"error:{e}", "matches": []}


# ═══════════════════════════════════════════════════
# React Health Search (DOB in cached patient list)
# ═══════════════════════════════════════════════════

def search_rh(rh_patients, last, first, schedule_dob):
    """Search pre-loaded RH patient list with DOB check."""
    matches = []
    for p in rh_patients:
        if name_match(first, last, p.get("first_name", ""), p.get("last_name", "")):
            pdob = p.get("birth_date", "")
            dob_ok = dob_matches(pdob, schedule_dob) if pdob else False
            matches.append({
                "id": p["id"],
                "name": f"{p.get('last_name', '')}, {p.get('first_name', '')}",
                "dob": pdob,
                "dob_verified": dob_ok,
                "serial": p.get("current_serial", ""),
                "last_report": p.get("last_report", ""),
                "portal": "RH",
            })
    return [m for m in matches if m["dob_verified"]]


# ═══════════════════════════════════════════════════
# Main Search Loop
# ═══════════════════════════════════════════════════

STATE_FILE = "/home/claude/search_state.json"


def save_state(output, last_completed, total):
    from datetime import datetime, timezone
    with open(STATE_FILE, "w") as f:
        json.dump({"last_completed_index": last_completed, "total": total,
                   "output": output,
                   "timestamp": datetime.now(timezone.utc).isoformat()}, f, indent=2)


def checkpoint_and_exit(next_offset, reason):
    """
    Session expired mid-chunk. Save checkpoint and exit with resume instructions.
    NEVER call input() — bash_tool is non-interactive and will hang/EOF.
    NEVER call auth_av_trigger here — that is the user's job between chunks (Phase 2).
    """
    print(f"\n  ⚠️ {reason} at patient index {next_offset}", flush=True)
    print(f"  Checkpoint saved. To resume:", flush=True)
    print(f"    1. python scripts/auth_co_rh.py", flush=True)
    print(f"    2. python scripts/auth_av.py", flush=True)
    print(f"    3. python scripts/auth_av_verify.py <CODE>", flush=True)
    print(f"    4. python scripts/search_all.py --offset {next_offset} --limit 20", flush=True)
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", default="/home/claude/patient_queue.json")
    parser.add_argument("--output", "-o", default="/home/claude/search_results.json")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="Resume from search_state.json")
    args = parser.parse_args()

    if args.resume and os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
        args.offset = state["last_completed_index"] + 1
        args.output = state.get("output", args.output)
        print(f"Resuming from patient {args.offset + 1} (last completed: {state['last_completed_index'] + 1})", flush=True)

    with open(args.queue) as f:
        queue_data = json.load(f)
    all_patients = queue_data["queued"]

    start = args.offset
    end = (start + args.limit) if args.limit > 0 else len(all_patients)
    patients = all_patients[start:end]
    print(f"Searching patients {start+1}-{min(end, len(all_patients))} of {len(all_patients)} across 3 portals\n", flush=True)

    av_data = load_session("av_session")
    co_data = load_session("co_session")
    rh_data = load_session("rh_session")

    av_session = av_data["session"] if av_data else None
    av_auth_time = av_data["auth_time"] if av_data else 0
    av_expired = av_session is None

    co_session = co_data["session"] if co_data else None
    co_headers = co_data["headers"] if co_data else None
    rh_patients = rh_data.get("patients", []) if rh_data else []

    creds = None  # lazy-load only if re-auth needed

    existing = []
    if args.offset > 0 and os.path.exists(args.output):
        with open(args.output) as f:
            existing = json.load(f)

    results = []
    SAVE_INTERVAL = 10

    for i, p in enumerate(patients):
        last = p["last_name"]
        first = p["first_name"]
        dob = p["dob"]
        entry = {"last": last, "first": first, "dob": dob,
                 "mrn": p.get("mrn", ""),
                 "visit_date": p.get("visit_date", ""),
                 "appt_notes": p.get("appt_notes", ""), "provider": p.get("provider", ""),
                 "av": [], "co": [], "rh": [],
                 "av_status": "", "co_status": "", "rh_status": ""}

        # Proactive AV session check at 12 min
        if not av_expired and av_session and (time.time() - av_auth_time > 720):
            if not check_av_session(av_session):
                av_expired = True
                print(f"  ⚠️ AV session expired at patient #{start + i + 1}", flush=True)

        # Mid-search AV expiry: checkpoint and exit — user re-auths between chunks.
        if av_expired:
            combined = existing + results
            with open(args.output, "w") as f:
                json.dump(combined, f, indent=2, default=str)
            save_state(args.output, start + i - 1, len(all_patients))
            checkpoint_and_exit(start + i, "AV session expired")

        # --- AirView ---
        if not av_expired and av_session:
            try:
                av_results = search_airview(av_session, last, first, dob)
                if av_results is None:
                    av_expired = True
                    entry["av_status"] = "SESSION_EXPIRED"
                else:
                    entry["av"] = av_results
                    verified = [r for r in av_results if r.get("dob_verified")]
                    entry["av_status"] = f"VERIFIED_{len(verified)}" if verified else (
                        f"FOUND_{len(av_results)}_NO_DOB_MATCH" if av_results else "NOT_FOUND")
            except Exception as e:
                entry["av_status"] = f"ERROR:{e}"
                print(f"  ⚠️ AV error on {last}: {e}", flush=True)
            time.sleep(0.8)
        else:
            entry["av_status"] = "SESSION_EXPIRED" if av_expired else "NOT_CONFIGURED"

        # --- Care Orchestrator ---
        if co_session and co_headers:
            try:
                co_result = search_co(co_session, co_headers, last, first, dob)
                entry["co"] = co_result["matches"]
                entry["co_status"] = f"VERIFIED_{len(co_result['matches'])}" if co_result["matches"] else (
                    co_result["status"] if co_result["status"] != "ok" else "NOT_FOUND")
            except Exception as e:
                entry["co_status"] = f"ERROR:{e}"
                print(f"  ⚠️ CO error on {last}: {e}", flush=True)
            time.sleep(0.5)
        else:
            entry["co_status"] = "NOT_CONFIGURED"

        # --- React Health ---
        try:
            rh_results = search_rh(rh_patients, last, first, dob)
            entry["rh"] = rh_results
            entry["rh_status"] = f"VERIFIED_{len(rh_results)}" if rh_results else "NOT_FOUND"
        except Exception as e:
            entry["rh_status"] = f"ERROR:{e}"
            print(f"  ⚠️ RH error on {last}: {e}", flush=True)

        results.append(entry)
        print(f"[{start + i + 1}/{len(all_patients)}] {last}, {first}: AV={entry['av_status']} CO={entry['co_status']} RH={entry['rh_status']}", flush=True)

        if (i + 1) % SAVE_INTERVAL == 0:
            combined = existing + results
            with open(args.output, "w") as f:
                json.dump(combined, f, indent=2, default=str)
            save_state(args.output, start + i, len(all_patients))
            print(f"  [checkpoint saved at {start + i + 1}]", flush=True)

    combined = existing + results
    with open(args.output, "w") as f:
        json.dump(combined, f, indent=2, default=str)
    save_state(args.output, start + len(results) - 1, len(all_patients))

    av_found = sum(1 for r in results if any(p.get("dob_verified") for p in r["av"]))
    co_found = sum(1 for r in results if r["co"])
    rh_found = sum(1 for r in results if r["rh"])
    any_found = sum(1 for r in results if any(p.get("dob_verified") for p in r["av"]) or r["co"] or r["rh"])
    none_found = len(results) - any_found

    print(f"\n{'=' * 50}")
    print(f"SEARCH COMPLETE: {len(results)} patients")
    print(f"  AirView (DOB verified): {av_found}")
    print(f"  Care Orchestrator:      {co_found}")
    print(f"  React Health:           {rh_found}")
    print(f"  Found on any portal:    {any_found}")
    print(f"  Not found anywhere:     {none_found}")
    print(f"\nSaved to: {args.output}")


if __name__ == "__main__":
    main()
