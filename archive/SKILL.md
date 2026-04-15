#!/usr/bin/env python3
"""
pap-compliance/scripts/parse_schedule.py

Parse an Epic DAR or reconciliation schedule PDF into a patient queue.
Applies skip rules from schedule_parsing.md.

Usage:
    python scripts/parse_schedule.py /path/to/schedule.pdf [--output /path/to/queue.json]

Output: JSON file with {"queued": [...], "skipped": [...], "mode": "batch"|"reconciliation"}
"""

import sys
import os
import re
import json
import argparse

def install_deps():
    os.system("pip install pdfplumber pdf2image pytesseract --break-system-packages -q 2>/dev/null")

install_deps()
import pdfplumber


# ═══════════════════════════════════════════════════
# Skip Rules
# ═══════════════════════════════════════════════════

def apply_skip_rules(patient):
    """Return (should_skip: bool, reason: str) for a patient dict."""
    notes = (patient.get("notes") or "").lower()
    ptype = (patient.get("type") or "").lower()
    provider = (patient.get("provider") or "").lower()

    # Already handled
    if notes.endswith("- ws"):
        return True, "already_done_ws"
    if re.search(r'dl\s+in\s+chart', notes, re.IGNORECASE):
        return True, "already_downloaded"

    # Tech / Download workflow
    if "tech_sdc_monu" in provider or "tech_sdc" in provider:
        return True, "tech_workflow"
    if ptype in ("download", "remote download"):
        return True, "tech_workflow"
    if "dl resmed dr" in notes and "tech" in provider:
        return True, "tech_workflow"

    # WatchPAT
    if "watchpat" in notes or "watch pat" in notes:
        return True, "watchpat"

    # Inspire
    if "inspire" in notes:
        return True, "inspire"

    # Not PAP
    if "not using" in notes or "no pap" in notes:
        return True, "not_using_pap"
    if "lvm to switch" in notes:
        return True, "switching_device"

    # New Patient with NP-style notes (no PAP data yet)
    if "new patient" in ptype:
        np_patterns = [
            r'np\s+ref[d.]?\s+by', r'np,?\s+referred', r'np\s+self\s*ref',
            r'np\s+refd\s+by', r'np\s+ref\.\s+by', r'np\s+ref\s+by',
            r'new\s+patient.*ref', r'np,?\s*snoring', r'np,?\s*ref',
            r'n/?p\s+self\s+referr', r'mp,?\s+referred',
        ]
        for pat in np_patterns:
            if re.search(pat, notes):
                return True, "new_patient_np"
        # Also skip new patients with no notes suggesting existing PAP
        if not any(kw in notes for kw in ["adherence", "follow up", "follow-up", "yearly",
                                            "cpap", "pap", "compliance", "dl in chart",
                                            "bring device", "consent to vv"]):
            return True, "new_patient_no_pap"

    # Medication only
    if re.match(r'^medication\s', notes) or notes == "medication follow up":
        if "yearly" not in notes and "adherence" not in notes and "pap" not in notes:
            return True, "medication_only"

    # DOWNLOAD type (tech)
    if ptype == "download" or ("download" in ptype and "8902" in str(patient.get("type_code", ""))):
        return True, "tech_download"

    return False, ""


# ═══════════════════════════════════════════════════
# PDF Parsing
# ═══════════════════════════════════════════════════

def detect_mode(text):
    """Detect batch DAR vs reconciliation schedule."""
    if "data abstraction dar" in text.lower() or "combined departments" in text.lower():
        return "batch"
    if "clinic schedule" in text.lower() or "daily schedule" in text.lower():
        return "reconciliation"
    # Default: if multi-day, batch
    dates = re.findall(r'\d{1,2}/\d{1,2}/\d{4}', text[:500])
    if len(set(dates)) > 2:
        return "batch"
    return "reconciliation"


def parse_dar_table(pdf_path):
    """Parse a batch DAR PDF. Returns list of patient dicts."""
    patients = []
    header_map = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Detect header row
                header_row = table[0]
                if header_row and any("patient" in str(c).lower() for c in header_row if c):
                    header_map = {}
                    for i, cell in enumerate(header_row):
                        c = str(cell).lower().strip() if cell else ""
                        if "visit" in c and "date" in c:
                            header_map["visit_date"] = i
                        elif "mrn" in c:
                            header_map["mrn"] = i
                        elif "time" in c:
                            header_map["time"] = i
                        elif "arrival" in c or "status" in c:
                            header_map["arrival_status"] = i
                        elif "patient" in c:
                            header_map["patient"] = i
                        elif "dob" in c:
                            header_map["dob"] = i
                        elif "type" in c:
                            header_map["type"] = i
                        elif "appt" in c or "note" in c:
                            header_map["notes"] = i
                        elif "provider" in c or "resource" in c:
                            header_map["provider"] = i
                    start = 1
                else:
                    start = 0

                if not header_map:
                    continue

                for row in table[start:]:
                    if not row or len(row) < max(header_map.values(), default=0) + 1:
                        continue

                    def get(key):
                        idx = header_map.get(key)
                        if idx is not None and idx < len(row):
                            return str(row[idx]).strip() if row[idx] else ""
                        return ""

                    patient_name = get("patient")
                    if not patient_name or patient_name.lower() in ("patient", ""):
                        continue

                    # Parse name: "Last, First Middle" or "Last, First M "Nickname""
                    name_clean = re.sub(r'"[^"]*"', '', patient_name).strip()
                    parts = name_clean.split(",", 1)
                    if len(parts) == 2:
                        last = parts[0].strip()
                        first = parts[1].strip().split()[0] if parts[1].strip() else ""
                    else:
                        last = patient_name
                        first = ""

                    # Clean up suffixes from last name for search but keep original
                    last_search = re.sub(r'\s+(Jr\.?|Sr\.?|II|III|IV)$', '', last, flags=re.IGNORECASE)

                    p = {
                        "last": last,
                        "first": first,
                        "last_search": last_search,
                        "full_name": patient_name,
                        "dob": get("dob"),
                        "mrn": get("mrn"),
                        "visit_date": get("visit_date"),
                        "time": get("time"),
                        "type": get("type"),
                        "notes": get("notes"),
                        "provider": get("provider"),
                        "arrival_status": get("arrival_status"),
                    }
                    patients.append(p)

    return patients


def parse_dar_ocr(pdf_path):
    """Parse image-based PDF using OCR. Fallback when pdfplumber finds no text."""
    from pdf2image import convert_from_path
    import pytesseract

    patients = []
    page_count = 0

    # Count pages first
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)

    print(f"OCR processing {total_pages} pages at 150 DPI (~10s per page)...")

    # Process in batches to avoid memory issues
    batch_size = 5
    for batch_start in range(1, total_pages + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, total_pages)
        images = convert_from_path(pdf_path, dpi=150,
                                   first_page=batch_start, last_page=batch_end)

        for img in images:
            page_count += 1
            text = pytesseract.image_to_string(img, config='--psm 6')
            page_patients = _parse_ocr_text(text)
            patients.extend(page_patients)
            print(f"  Page {page_count}/{total_pages}: {len(page_patients)} patients found")

    return patients


def _parse_ocr_text(text):
    """Parse OCR text from a single page into patient records."""
    lines = text.split('\n')
    patients = []

    # Join all lines into one big string, then split on visit-date boundaries
    # Each patient entry starts with a date like "03/30/2026"
    full_text = ' '.join(l.strip() for l in lines if l.strip())

    # Remove header lines
    full_text = re.sub(r'Department Appointments Report.*?Provider/?Resource', '', full_text)
    full_text = re.sub(r'Printed on.*?Page \d+ of \d+', '', full_text)
    full_text = re.sub(r'Visit Date.*?Provider/?Resource', '', full_text)

    # Split on visit date pattern (MM/DD/YYYY followed by MRN-like digits)
    # Each entry starts with a date
    entry_pattern = r'(\d{2}/\d{2}/\d{4})[)\]|]?\s*(\d{8,9})'
    splits = list(re.finditer(entry_pattern, full_text))

    for idx, match in enumerate(splits):
        visit_date = match.group(1)
        mrn = match.group(2)

        # Get text from this match to next match (or end)
        start = match.end()
        end = splits[idx + 1].start() if idx + 1 < len(splits) else len(full_text)
        chunk = full_text[start:end].strip()

        # Clean OCR artifacts
        chunk = re.sub(r'[|]', ' ', chunk)
        chunk = re.sub(r'\bO\s+Confirmed\b', 'Confirmed', chunk)  # OCR checkbox → "O Confirmed"
        chunk = re.sub(r'[OQ0]\s*\)\s*Confirmed', 'Confirmed', chunk)  # "O) Confirmed"
        chunk = re.sub(r'Cc?\s*\)\s*Unconfirmed', 'Unconfirmed', chunk)
        chunk = re.sub(r'\s+', ' ', chunk)

        # Also try to match names that appear after AM/PM without Confirmed/Unconfirmed
        # (when OCR drops the status word entirely)

        # Extract patient name: "Last, First" — appears after Confirmed/Unconfirmed
        name_match = re.search(
            r'(?:Confirmed|Unconfirmed)\s+([A-Z][a-zA-Z\'-]+(?:\s+[A-Z][a-z]*)*,\s*[A-Z][a-zA-Z\'-]+(?:\s+[A-Z]\.?)?)',
            chunk)
        if not name_match:
            # Fallback 1: after AM/PM + any word
            name_match = re.search(r'[AP]M\s+\w+\s+([A-Z][a-zA-Z\'-]+,\s*[A-Z][a-zA-Z\'-]+)', chunk)
        if not name_match:
            # Fallback 2: any "Capitalized, Capitalized" followed by a date (DOB)
            name_match = re.search(
                r'([A-Z][a-zA-Z\'-]+(?:\s+[A-Z][a-z]*)*,\s*[A-Z][a-zA-Z\'-]+(?:\s+[A-Z]\.?)?)\s+\d{2}/\d{2}/\d{4}',
                chunk)
        if not name_match:
            # Fallback 3: broadest — "Word, Word" anywhere in chunk
            name_match = re.search(r'([A-Z][a-z]{2,},\s*[A-Z][a-z]{2,})', chunk)

        if not name_match:
            continue

        full_name = name_match.group(1).strip()
        # Remove trailing "O" or single chars that are OCR noise
        full_name = re.sub(r'\s+[A-Z]$', '', full_name)

        parts = full_name.split(",", 1)
        if len(parts) < 2:
            continue
        last = parts[0].strip()
        first_raw = parts[1].strip()
        first = first_raw.split()[0] if first_raw.split() else ""

        # Skip if name looks like a header or garbage
        if last.lower() in ('patient', 'visit', 'date', 'status', 'type'):
            continue

        # Extract DOB: date after the name that's clearly a birth date (year < 2020)
        dob = ""
        name_end = name_match.end()
        after_name = chunk[name_end - match.end():]
        dob_candidates = re.findall(r'(\d{2}/\d{2}/\d{4})', after_name)
        for d in dob_candidates:
            try:
                yr = int(d.split('/')[-1])
                if yr < 2020:
                    dob = d
                    break
            except:
                pass

        # Extract type: ANY, New Patient, VV MC OV, TECH, DOWNLOAD
        ptype = ""
        type_match = re.search(
            r'(ANY|New Patient|VV\s*MC?\s*OV|TECH|DOWNLOAD)\s*(?:\[\d+\])?',
            after_name, re.IGNORECASE)
        if type_match:
            ptype = type_match.group(1).strip()

        # Everything after type/DOB is notes + provider
        notes = ""
        provider = ""
        # Find provider at end (known names)
        prov_match = re.search(
            r'(Katch?inoff|Bashir|Bond|Santos)[,\s]+(\w+)\s*(?:\[\d+\])?\s*$',
            chunk, re.IGNORECASE)
        if prov_match:
            provider = f"{prov_match.group(1)}, {prov_match.group(2)}"
            # Notes are between type/DOB and provider
            notes_start = name_match.end() - match.end()
            notes_end = prov_match.start() - (match.end() - start)
            notes_region = chunk[max(0, notes_start):].split(provider)[0] if provider else ""
            # Clean up notes
            notes = re.sub(r'^\s*\d{2}/\d{2}/\d{4}\s*', '', notes_region)
            notes = re.sub(r'^(ANY|New Patient|VV\s*MC?\s*OV|TECH|DOWNLOAD)\s*(\[\d+\])?\s*', '', notes, flags=re.IGNORECASE)
            notes = notes.strip()

        p = {
            "last": last,
            "first": first,
            "last_search": re.sub(r'\s+(Jr\.?|Sr\.?|II|III|IV)$', '', last, flags=re.IGNORECASE),
            "full_name": full_name,
            "dob": dob,
            "mrn": mrn,
            "visit_date": visit_date,
            "time": "",
            "type": ptype,
            "notes": notes[:200],
            "provider": provider,
            "arrival_status": "",
        }
        patients.append(p)

    return patients


def pdf_has_text(pdf_path):
    """Check if PDF has extractable text (not image-only)."""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[:3]:
            if page.chars:
                return True
    return False


def parse_schedule(pdf_path):
    """Main entry point. Returns (mode, queued, skipped).
    Uses pdfplumber for text PDFs, OCR for image-based PDFs.
    """
    has_text = pdf_has_text(pdf_path)

    if has_text:
        with pdfplumber.open(pdf_path) as pdf:
            first_page_text = pdf.pages[0].extract_text() or ""
        mode = detect_mode(first_page_text)
        raw_patients = parse_dar_table(pdf_path)
        print(f"Extracted {len(raw_patients)} rows via pdfplumber (text PDF)")
    else:
        print("Image-based PDF detected — using OCR (this takes ~30-60 seconds)...")
        raw_patients = parse_dar_ocr(pdf_path)
        mode = "batch"  # DAR is always batch
        print(f"Extracted {len(raw_patients)} rows via OCR")

    if not raw_patients:
        print("\n⚠️  No patients extracted from PDF.")
        print("This PDF may need manual parsing. Claude should read the PDF from context")
        print("and build patient_queue.json manually using this template:")
        print(json.dumps({
            "mode": "batch", "queued": [
                {"last": "LAST", "first": "FIRST", "dob": "MM/DD/YYYY",
                 "visit_date": "M/D/YYYY", "notes": "appt notes", "provider": "Provider, Name",
                 "mrn": "", "type": "", "time": "", "arrival_status": "",
                 "last_search": "LAST", "full_name": "LAST, FIRST"}
            ], "skipped": []
        }, indent=2))
        return mode, [], []

    # Deduplicate by MRN (same patient might appear if multi-visit)
    seen_mrns = set()
    unique = []
    for p in raw_patients:
        key = p["mrn"] or f"{p['last']}_{p['first']}_{p['dob']}"
        if key not in seen_mrns:
            seen_mrns.add(key)
            unique.append(p)

    queued = []
    skipped = []
    for p in unique:
        should_skip, reason = apply_skip_rules(p)
        if should_skip:
            skipped.append({**p, "skip_reason": reason})
        else:
            queued.append(p)

    return mode, queued, skipped


# ═══════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Parse Epic schedule PDF into patient queue")
    parser.add_argument("pdf", help="Path to schedule PDF")
    parser.add_argument("--output", "-o", default="/home/claude/patient_queue.json",
                        help="Output JSON path")
    args = parser.parse_args()

    if not os.path.exists(args.pdf):
        print(f"ERROR: File not found: {args.pdf}")
        sys.exit(1)

    mode, queued, skipped = parse_schedule(args.pdf)

    result = {
        "mode": mode,
        "source_file": args.pdf,
        "parsed_at": __import__("datetime").datetime.now().isoformat(),
        "total_rows": len(queued) + len(skipped),
        "queued_count": len(queued),
        "skipped_count": len(skipped),
        "queued": queued,
        "skipped": skipped,
    }

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    # Summary
    print(f"Mode: {mode}")
    print(f"Total rows: {result['total_rows']}")
    print(f"Queued: {result['queued_count']}")
    print(f"Skipped: {result['skipped_count']}")

    # Skip reason breakdown
    reasons = {}
    for s in skipped:
        r = s["skip_reason"]
        reasons[r] = reasons.get(r, 0) + 1
    if reasons:
        print("\nSkip reasons:")
        for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {r}: {c}")

    print(f"\nQueued patients:")
    for p in queued:
        print(f"  {p['last']}, {p['first']} (DOB {p['dob']}) — {p['visit_date']} — {p['notes'][:60]}")

    print(f"\nSaved to: {args.output}")


if __name__ == "__main__":
    main()
