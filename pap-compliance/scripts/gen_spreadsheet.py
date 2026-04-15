#!/usr/bin/env python3
"""
pap-compliance/scripts/gen_spreadsheet.py

Generate PAP_Compliance_{date}.xlsx from search results and download log.
Summary sheet + per-day tabs. Color-coded statuses.

Usage:
    python scripts/gen_spreadsheet.py [--search /path/to/search_results.json] \
        [--downloads /path/to/download_log.json] [--output-dir /path/to/reports]
"""

import sys
import os
import json
import re
import argparse
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from utils import parse_avail_days, is_stale, REPORTS_DIR, OUTPUTS_DIR

# Colors
GREEN = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
YELLOW = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
RED = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
GRAY = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
THIN_BORDER = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin'))


def get_status(patient, dl_lookup):
    """Determine status, fill color, and file list for a patient."""
    name = f"{patient['last']}, {patient['first']}"

    # Check download log
    if name in dl_lookup:
        entry = dl_lookup[name]
        r30 = entry.get("report_30", {})
        r90 = entry.get("report_90")
        files = []
        if r30 and r30.get("status") == "OK":
            files.append(r30.get("file", ""))
        if r90 and r90.get("status") == "OK":
            files.append(r90.get("file", ""))
        if files:
            return "Downloaded", GREEN, ", ".join(files)
        return "Download failed", RED, ""

    # Check search results
    av_profiles = patient.get("av", [])
    co_matches = patient.get("co", [])
    rh_matches = patient.get("rh", [])

    if not av_profiles and not co_matches and not rh_matches:
        return "Not found", RED, ""

    # CO found but no AV download (report gen broken)
    if co_matches and not any(p.get("dob_verified") for p in av_profiles):
        return "CO only (manual pull)", YELLOW, ""

    # AV profiles exist but none verified
    verified = [p for p in av_profiles if p.get("dob_verified")]
    if not verified:
        # Check why
        for p in av_profiles:
            reason = p.get("skip_reason", "")
            if "stale" in reason:
                return f"Stale ({p.get('updated', '')})", YELLOW, ""
            if "zero_usage" in reason:
                return "0% usage", YELLOW, ""
            if "dob_mismatch" in reason:
                return "DOB mismatch (skipped)", RED, ""
        if is_stale(av_profiles[0].get("updated", "--")) if av_profiles else False:
            return "Bring device - data stale", YELLOW, ""
        return "Not downloadable", GRAY, ""

    # Verified but not downloaded — check available days
    best = verified[0]
    avail = parse_avail_days(best.get("available", "0"))
    if avail < 30:
        return f"Insufficient ({avail}d)", GRAY, ""
    if best.get("last30") == "0%":
        return "0% usage", YELLOW, ""

    return "Queued (not downloaded)", GRAY, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", default="/home/claude/search_results.json")
    parser.add_argument("--downloads", default=os.path.join(REPORTS_DIR, "download_log.json"))
    parser.add_argument("--output-dir", default=REPORTS_DIR)
    parser.add_argument("--zip", action="store_true", help="Also create ZIP of all deliverables")
    args = parser.parse_args()

    with open(args.search) as f:
        search_results = json.load(f)

    dl_lookup = {}
    if os.path.exists(args.downloads):
        with open(args.downloads) as f:
            for entry in json.load(f):
                dl_lookup[entry["patient"]] = entry

    today = datetime.now().strftime("%m_%d_%Y")
    wb = Workbook()

    # Summary sheet
    ws = wb.active
    ws.title = "Summary"
    headers = ["Patient", "DOB", "Visit Date", "Provider", "Status", "Files", "Notes"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal='center')
        cell.border = THIN_BORDER

    row = 2
    for p in search_results:
        name = f"{p['last']}, {p['first']}"
        status, fill, files = get_status(p, dl_lookup)

        ws.cell(row=row, column=1, value=name).border = THIN_BORDER
        ws.cell(row=row, column=2, value=p["dob"]).border = THIN_BORDER
        ws.cell(row=row, column=3, value=p.get("visit_date", "")).border = THIN_BORDER
        ws.cell(row=row, column=4, value=p.get("provider", "")).border = THIN_BORDER
        sc = ws.cell(row=row, column=5, value=status)
        sc.fill = fill
        sc.border = THIN_BORDER
        ws.cell(row=row, column=6, value=files).border = THIN_BORDER
        ws.cell(row=row, column=7, value=p.get("notes", "")[:80]).border = THIN_BORDER
        row += 1

    # Auto-width
    for col in range(1, len(headers) + 1):
        max_len = len(headers[col - 1])
        for r in range(2, row):
            val = ws.cell(row=r, column=col).value
            if val:
                max_len = max(max_len, len(str(val)))
        ws.column_dimensions[get_column_letter(col)].width = min(max_len + 2, 50)

    # Per-day sheets
    days = {}
    for p in search_results:
        vd = p.get("visit_date", "unknown")
        if vd not in days:
            days[vd] = []
        days[vd].append(p)

    for day, pts in sorted(days.items()):
        safe = day.replace("/", "-") if day else "unknown"
        dws = wb.create_sheet(title=safe[:31])
        day_h = ["Patient", "DOB", "Provider", "Status", "Files", "Notes"]
        for col, h in enumerate(day_h, 1):
            cell = dws.cell(row=1, column=col, value=h)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal='center')
            cell.border = THIN_BORDER

        drow = 2
        for p in pts:
            name = f"{p['last']}, {p['first']}"
            status, fill, files = get_status(p, dl_lookup)
            dws.cell(row=drow, column=1, value=name).border = THIN_BORDER
            dws.cell(row=drow, column=2, value=p["dob"]).border = THIN_BORDER
            dws.cell(row=drow, column=3, value=p.get("provider", "")).border = THIN_BORDER
            sc = dws.cell(row=drow, column=4, value=status)
            sc.fill = fill
            sc.border = THIN_BORDER
            dws.cell(row=drow, column=5, value=files).border = THIN_BORDER
            dws.cell(row=drow, column=6, value=p.get("notes", "")[:80]).border = THIN_BORDER
            drow += 1

        for col in range(1, len(day_h) + 1):
            max_len = len(day_h[col - 1])
            for r in range(2, drow):
                val = dws.cell(row=r, column=col).value
                if val:
                    max_len = max(max_len, len(str(val)))
            dws.column_dimensions[get_column_letter(col)].width = min(max_len + 2, 50)

    xlsx_path = os.path.join(args.output_dir, f"PAP_Compliance_{today}.xlsx")
    wb.save(xlsx_path)
    print(f"Spreadsheet: {xlsx_path}")

    # Generate run log
    log_lines = [
        f"PAP Compliance Run Log — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60, "",
    ]

    statuses = {}
    for p in search_results:
        s, _, _ = get_status(p, dl_lookup)
        statuses[s] = statuses.get(s, 0) + 1
    log_lines.append("Status summary:")
    for s, c in sorted(statuses.items(), key=lambda x: -x[1]):
        log_lines.append(f"  {s}: {c}")

    log_path = os.path.join(args.output_dir, f"PAP_Compliance_Log_{today}.txt")
    with open(log_path, "w") as f:
        f.write("\n".join(log_lines))
    print(f"Log: {log_path}")

    # ZIP if requested
    if args.zip:
        zip_path = os.path.join(OUTPUTS_DIR, f"PAP_Compliance_{today}.zip")
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for fname in sorted(os.listdir(args.output_dir)):
                fpath = os.path.join(args.output_dir, fname)
                if os.path.isfile(fpath):
                    zf.write(fpath, fname)
        print(f"ZIP: {zip_path}")

    # Print summary
    print(f"\nStatus counts:")
    for s, c in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {s}: {c}")


if __name__ == "__main__":
    main()
