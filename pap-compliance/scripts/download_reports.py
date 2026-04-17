#!/usr/bin/env python3
"""
pap-compliance/scripts/download_reports.py

Download compliance reports for all DOB-verified patients.
Handles 30-day + 90-day logic, session management, proactive re-auth.

Usage:
    python scripts/download_reports.py [--results /path/to/search_results.json] [--reports-dir /path/to/reports]
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
    parse_avail_days, is_stale, check_av_session, SESSION_DIR, REPORTS_DIR,
    download_co_sleep_trend, co_get_equipment_serial,
)
from datetime import datetime


def load_session(name):
    path = os.path.join(SESSION_DIR, f"{name}.pkl")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def download_av_report(av_session, ecn, portal_name, period, today_str, reports_dir):
    """Download a single AirView compliance report. Returns result dict."""
    timestamp = datetime.now().strftime("%m%d%y_%H%M%S")
    url = f"https://airview.resmed.com/patients/{ecn}/report/compliance/Compliance_report_{timestamp}.pdf"
    params = {
        "returningUrl": "/patients", "reportType": "Compliance",
        "reportPeriodType": "SUPPLIED",
        "reportingPeriodLength": str(period),
        "reportingPeriodEnd": today_str,
    }
    try:
        r = av_session.get(url, params=params, timeout=60)
        if r.status_code == 200 and r.content[:5] == b'%PDF-':
            suffix = " 90" if period == 90 else ""
            filename = f"{portal_name}{suffix}.pdf"
            with open(os.path.join(reports_dir, filename), "wb") as f:
                f.write(r.content)
            return {"status": "OK", "file": filename, "size": len(r.content)}
        else:
            # Capture a body snippet so "HTML instead of PDF" failures are
            # diagnosable — the AirView SUPPLIED 30-day path does this occasionally,
            # and without the snippet we can't tell if it was a session boot, a
            # Cloudflare challenge, or a real backend error.
            try:
                snippet = r.content[:200].decode("utf-8", errors="replace")
            except Exception:
                snippet = repr(r.content[:200])
            return {"status": "FAIL", "code": r.status_code,
                    "size": len(r.content), "body_snippet": snippet}
    except Exception as e:
        return {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}


DL_STATE_FILE = "/home/claude/download_state.json"


def save_dl_state(last_completed, total):
    from datetime import datetime, timezone
    with open(DL_STATE_FILE, "w") as f:
        json.dump({"last_completed_index": last_completed, "total": total,
                   "timestamp": datetime.now(timezone.utc).isoformat()}, f, indent=2)


def checkpoint_and_exit(next_offset, total, reason):
    """
    Session expired mid-chunk. Save checkpoint and exit with resume instructions.
    NEVER call input() — bash_tool is non-interactive and will hang/EOF.
    NEVER call auth_av_trigger here — the user re-auths between chunks (Phase 2).
    """
    save_dl_state(next_offset - 1, total)
    print(f"\n  ⚠️ {reason} at download index {next_offset}", flush=True)
    print(f"  Checkpoint saved. To resume:", flush=True)
    print(f"    1. python scripts/auth_co_rh.py", flush=True)
    print(f"    2. python scripts/auth_av.py", flush=True)
    print(f"    3. python scripts/auth_av_verify.py <CODE>", flush=True)
    print(f"    4. python scripts/download_reports.py --resume", flush=True)
    sys.exit(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="/home/claude/search_results.json")
    parser.add_argument("--reports-dir", default=REPORTS_DIR)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="Resume from download_state.json")
    args = parser.parse_args()

    if args.resume and os.path.exists(DL_STATE_FILE):
        with open(DL_STATE_FILE) as f:
            state = json.load(f)
        args.offset = state["last_completed_index"] + 1
        print(f"Resuming from download #{args.offset + 1} (last completed: {state['last_completed_index'] + 1})", flush=True)

    os.makedirs(args.reports_dir, exist_ok=True)

    with open(args.results) as f:
        search_results = json.load(f)

    av_data = load_session("av_session")
    if not av_data:
        print("ERROR: No AV session. Run auth scripts first.")
        sys.exit(1)
    av_session = av_data["session"]
    av_auth_time = av_data["auth_time"]

    if not check_av_session(av_session):
        print("ERROR: AV session expired. Need re-auth.")
        sys.exit(1)

    today_str = datetime.now().strftime("%m/%d/%Y")
    creds = None
    print(f"AV session live. Starting downloads...\n", flush=True)

    full_queue = []
    for r in search_results:
        verified_av = [p for p in r.get("av", []) if p.get("dob_verified")]
        if verified_av:
            best = verified_av[0]
            avail_days = parse_avail_days(best.get("available", "0"))
            if avail_days < 30:
                continue
            if is_stale(best.get("updated", "--")) and best.get("last30") == "0%":
                continue
            full_queue.append({
                "name": f"{r['last']}, {r['first']}",
                "dob": r["dob"],
                "portal_name": best["name"],
                "ecn": best["ecn"],
                "avail_days": avail_days,
                "last30": best.get("last30", ""),
            })

    start = args.offset
    end = (start + args.limit) if args.limit > 0 else len(full_queue)
    queue = full_queue[start:end]
    print(f"Download queue: {len(full_queue)} total, processing {start+1}-{min(end, len(full_queue))}\n", flush=True)

    log_path = os.path.join(args.reports_dir, "download_log.json")
    existing_log = []
    if args.offset > 0 and os.path.exists(log_path):
        with open(log_path) as f:
            existing_log = json.load(f)

    log = []
    downloaded = 0
    failed = 0

    for i, p in enumerate(queue):
        if i % 5 == 0 or time.time() - av_auth_time > 720:
            if not check_av_session(av_session):
                with open(log_path, "w") as f:
                    json.dump(existing_log + log, f, indent=2, default=str)
                checkpoint_and_exit(start + i, len(full_queue), "AV session expired")

        try:
            r30 = download_av_report(av_session, p["ecn"], p["portal_name"], 30, today_str, args.reports_dir)
            time.sleep(0.8)
            r90 = None
            if p["avail_days"] >= 90:
                r90 = download_av_report(av_session, p["ecn"], p["portal_name"], 90, today_str, args.reports_dir)
                time.sleep(0.8)

            entry = {"patient": p["name"], "dob": p["dob"], "portal_name": p["portal_name"],
                     "ecn": p["ecn"], "avail_days": p["avail_days"],
                     "report_30": r30, "report_90": r90}
            log.append(entry)
            r30s = r30["status"]
            r90s = r90["status"] if r90 else "N/A"
            if r30s == "OK" or (r90 and r90["status"] == "OK"):
                downloaded += 1
            else:
                failed += 1
            print(f"[{start + i + 1}/{len(full_queue)}] {p['name']} — 30d:{r30s} 90d:{r90s}", flush=True)

        except Exception as e:
            log.append({"patient": p["name"], "status": f"ERROR:{e}"})
            failed += 1
            print(f"[{start + i + 1}/{len(full_queue)}] {p['name']} — ERROR: {e}", flush=True)

        save_dl_state(start + i, len(full_queue))

    with open(log_path, "w") as f:
        json.dump(existing_log + log, f, indent=2, default=str)

    # ── CO Sleep Trend pass ────────────────────────────────────────────
    # Best-effort CO downloads for patients whose CO search was DOB-verified.
    # The POST body to documents-v1-0-server is a best guess (see utils.
    # download_co_sleep_trend); if the server rejects it, the full response is
    # appended to /home/claude/co_generate_capture.json so the next iteration
    # has a narrow target. This path does NOT block AV downloads — AV is done
    # by the time we get here.
    co_data = load_session("co_session")
    co_downloaded = 0
    co_failed = 0
    co_log = []
    if co_data:
        co_session = co_data["session"]
        co_headers = co_data["headers"]
        co_queue = []
        for r in search_results:
            verified_co = [p for p in r.get("co", []) if p.get("dob_verified")]
            if verified_co:
                best = verified_co[0]
                co_queue.append({
                    "name": f"{r['last']}, {r['first']}",
                    "dob": r["dob"],
                    "patient_uuid": best["patientId"],
                })
        if co_queue:
            print(f"\nCO Sleep Trend pass: {len(co_queue)} patient(s) verified on CO")
        for j, p in enumerate(co_queue):
            serial, eq_info = co_get_equipment_serial(co_session, co_headers,
                                                      p["patient_uuid"])
            if not serial:
                print(f"  [{j+1}/{len(co_queue)}] {p['name']} — equipment lookup failed: "
                      f"{eq_info.get('error', 'unknown')}", flush=True)
                co_log.append({"patient": p["name"], "patient_uuid": p["patient_uuid"],
                               "status": "EQUIPMENT_FAIL", "detail": eq_info})
                co_failed += 1
                continue
            result = download_co_sleep_trend(co_session, co_headers,
                                             p["patient_uuid"], serial,
                                             days=30, reports_dir=args.reports_dir)
            co_log.append({"patient": p["name"], "patient_uuid": p["patient_uuid"],
                           "serial": serial, "result": result})
            status = result.get("status")
            if status == "OK":
                co_downloaded += 1
                print(f"  [{j+1}/{len(co_queue)}] {p['name']} — CO Sleep Trend: OK "
                      f"({result.get('method')}, {result.get('size', 0)} bytes)", flush=True)
            else:
                co_failed += 1
                print(f"  [{j+1}/{len(co_queue)}] {p['name']} — CO Sleep Trend: {status} — "
                      f"{result.get('error', '')} [capture: {result.get('capture_file', '')}]",
                      flush=True)

        # Append CO log to the main log file
        with open(log_path, "w") as f:
            json.dump(existing_log + log + co_log, f, indent=2, default=str)
    else:
        print("\nCO session not loaded — skipping CO Sleep Trend pass.", flush=True)

    pdf_count = len([f for f in os.listdir(args.reports_dir) if f.endswith(".pdf")])
    total_mb = sum(os.path.getsize(os.path.join(args.reports_dir, f))
                   for f in os.listdir(args.reports_dir) if f.endswith(".pdf")) / (1024 * 1024)

    print(f"\n{'=' * 50}")
    print(f"CHUNK COMPLETE: downloads {start+1}-{min(end, len(full_queue))} of {len(full_queue)}")
    print(f"  AV — patients with ≥1 successful PDF: {downloaded}")
    print(f"  AV — patients with failures only:     {failed}")
    if co_data:
        print(f"  CO — Sleep Trend successes:           {co_downloaded}")
        print(f"  CO — Sleep Trend failures:            {co_failed}")
        if co_failed:
            print(f"  CO capture (for debugging):           /home/claude/co_generate_capture.json")
    print(f"  PDFs on disk:                         {pdf_count} ({total_mb:.1f} MB)")
    print(f"  Log:                                  {log_path}")


if __name__ == "__main__":
    main()
