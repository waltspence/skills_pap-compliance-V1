---
name: pap-compliance
description: >
  Use this skill whenever the user wants to pull PAP compliance reports for a patient schedule.
  Triggers include: "pull compliance reports", "run AirView reports", "get CPAP reports",
  "download compliance reports", any mention of AirView / Care Orchestrator / React Health +
  patients/schedule, or any reference to this standing SOP. Always use this skill when an Epic
  appointment report (PDF or text) is provided alongside any intent to generate compliance reports —
  even if the user just says "let's run reports" or "start the report workflow".
  Covers three platforms: ResMed AirView, Philips Care Orchestrator, and React Health Connect.
---

# PAP Compliance Report — Standing SOP (v3)

Pull compliance reports from manufacturer portals for a clinic schedule.
Ready-to-run scripts in `scripts/` — run them instead of writing code from scratch.

## Editing this skill

`/mnt/skills/` is **read-only at runtime**. Do NOT try to `str_replace` files here when
asked to fix a bug — the edit will silently fail (or partially succeed into a tmp copy).
Instead, emit the fix as an **output prompt**: generate the new file contents plus an
`APPLY.md`, zip it into `/mnt/user-data/outputs/`, and hand it to the user to drop into
a fresh session. The user applies it between sessions.

## Execution Environment

All code runs via `bash_tool` in the claude.ai Linux container (non-interactive —
**never call `input()` in any script**; the shell will hang until timeout). Python
`requests` is pre-installed. Filesystem resets between sessions — save outputs to
`/mnt/user-data/outputs/`.

**Phase 0 — first-run setup (once per session):**
```bash
pip install pycryptodome beautifulsoup4 openpyxl pdfplumber --break-system-packages -q
```

If `parse_schedule.py` falls back to OCR (image-based PDF), it also needs
`pdf2image` + `pytesseract` and the system binaries `poppler-utils` + `tesseract-ocr`.
Install lazily only when OCR is triggered:
```bash
apt-get install -y poppler-utils tesseract-ocr
pip install pdf2image pytesseract --break-system-packages -q
```

**Tool mapping:**
- **File/PDF input:** Schedule PDF from uploads or context
- **Code execution (`bash_tool`):** ALL auth, search, download, spreadsheet
- **AskUserQuestion:** Credentials (first run only), MFA codes, review gate
- **Browser automation:** NOT used. All platforms use `requests` only.

## Bash budget & chunked execution

`bash_tool` has a ~2–3 minute execution limit. Any step that processes the full patient
queue in one call will be killed mid-run. All long-running scripts support
`--offset N --limit 20` and checkpoint to disk. **Default chunk size is 20 patients.**

- **Search checkpoint:** `/home/claude/search_results.json` + `search_state.json`
- **Download checkpoint:** `/home/claude/download_log.json` + `download_state.json`
- **Resume:** Both scripts read the state file on next invocation and advance automatically

On AV session expiry mid-chunk, scripts now **save checkpoint and `sys.exit(2)`** with
resume instructions. They do NOT auto-retrigger MFA — the user re-auths between chunks.

## Platforms & Report Types

| Platform | Manufacturer | Report | Endpoint Status |
|---|---|---|---|
| AirView | ResMed | Compliance and Therapy | ✅ Full pipeline |
| Care Orchestrator | Philips | Compliance / Trilogy Detail | ✅ Search, ⚠️ Report gen (see references/co_reports_api.md) |
| React Health | 3B | Compliance and Therapy | ✅ Auth/search, PDF needs work |

## Two Workflow Modes

Auto-detect from input format. See `references/schedule_parsing.md`.

**Batch** — 2-week lookahead DAR. Big run, 50–100+ patients.
**Reconciliation** — day-before clinic schedule. 3–6 patients.

## Workflow — Run Scripts in Order

### Phase 1 — Parse Schedule
```bash
python scripts/parse_schedule.py /mnt/user-data/uploads/schedule.pdf
```
Outputs `/home/claude/patient_queue.json` with queued + skipped patients.
Present summary to user, wait for confirmation.

### Phase 2 — Authenticate

**Step 1 — Credentials:** If `/home/claude/pap_creds.json` does not exist, use
`AskUserQuestion` to collect credentials. **Never paste passwords into bash commands
or heredocs** — they end up in the conversation transcript. Expected JSON shape once
written (via Python after AskUserQuestion returns):
```json
{
  "AirView":          {"email": "...",    "password": "..."},
  "CareOrchestrator": {"username": "...", "password": "..."},
  "ReactHealth":      {"email": "...",    "password": "..."}
}
```

**Step 2 — Auth CO + RH** (no side effects, instant):
```bash
python scripts/auth_co_rh.py
```
Output: `CO ✅, RH ✅`. CO auto-retries up to 3× with a fresh `requests.Session()`
and a freshly-computed AES key per attempt. If the RH cache is exactly 25 patients,
a ⚠️ warning prints — that likely indicates silent pagination truncation, capture the
endpoint shape before trusting RH results.

**Step 3 — Trigger AirView MFA** (run only when ready to enter the code):
```bash
python scripts/auth_av.py
```
Output: `AV MFA sent. Check email.`

**Step 4 — Verify MFA:**
```bash
python scripts/auth_av_verify.py <CODE>
```
Output: `AV: ✅ Session LIVE`

Auth split rationale: `auth_co_rh.py` never touches AirView, so CO retry loops cannot
flood the inbox with MFA emails. `auth_av.py` is the **only** path that triggers MFA.

**Key fix (March 2026):** `auth_av_trigger` captures the real OAuth state/nonce from
AirView's login redirect chain BEFORE triggering MFA. This prevents the CSRF mismatch
that previously consumed MFA codes.

### Phase 3 — Search All Portals (chunked, with DOB verify)

For queues ≤20 patients:
```bash
python scripts/search_all.py
```

For queues >20 patients, loop in chunks of 20 until the state file reports complete:
```bash
python scripts/search_all.py --offset 0  --limit 20
python scripts/search_all.py --offset 20 --limit 20
python scripts/search_all.py --offset 40 --limit 20
# ... until start + limit >= total
```

DOB is verified inline. Wrong-patient profiles are rejected before any download.
Output: `/home/claude/search_results.json` (merged across chunks).

If AV session expires mid-chunk, the script saves checkpoint and exits with code 2
plus resume instructions — re-run Phase 2 Steps 3+4, then rerun search with the
suggested `--offset`.

### Phase 3.5 — Review Queue

Categorize search results:
- **Downloadable:** DOB-verified, active data, ≥30 days
- **Auto-skip:** stale >3mo, 0% usage, <30 days, data >1yr ("Bring device")
- **CO only:** Found on CO but report gen broken — flag for manual pull
- **Not found:** Not on any portal

Present to user, get go/no-go.

### Phase 4 — Download Reports (chunked)

For ≤20 downloads:
```bash
python scripts/download_reports.py
```

For more:
```bash
python scripts/download_reports.py --offset 0  --limit 20
python scripts/download_reports.py --offset 20 --limit 20
# ... or use --resume to pick up from download_state.json automatically
```

Downloads 30-day (all) + 90-day (when ≥90 days available) from AirView.
Session check every 5 patients. On expiry: checkpoint + `sys.exit(2)` with resume steps.

Output: PDFs in `/home/claude/reports/` + `download_log.json`.

### Phase 5 — Spreadsheet + ZIP
```bash
python scripts/gen_spreadsheet.py --zip
```
Generates `PAP_Compliance_{YYYY-MM-DD}.xlsx` (summary + per-day tabs) + run log.
With `--zip`: creates the final archive in `/mnt/user-data/outputs/`.

**Output contract — the final ZIP contains:**
- `PAP_Compliance_{YYYY-MM-DD}.xlsx` — summary + per-day tabs
- `reports/*.pdf` — all downloaded compliance PDFs (from `/home/claude/reports/`)
- `run_log.json` — per-patient outcomes (queued, skipped, found, downloaded, errors)
- `download_log.json` — raw download results with HTTP codes and sizes
- `search_results.json` — merged search results across all chunks

Final ZIP path: `/mnt/user-data/outputs/PAP_Compliance_{YYYY-MM-DD}.zip`

### Phase 6 — Deliver
Call `present_files` with the ZIP path first. Give the user a concise summary
(counts: pulled / skipped / not found / manual-flagged) and nothing more.
The user can open the ZIP themselves — do not recap its contents in prose.

## Key Rules

1. **Verify DOB before downloading** — handled by `search_all.py` inline
2. **Always search all 3 portals** for every patient — no manufacturer routing from notes
3. **Always get ALL profiles**, sort by recency, try each in order
4. **Strict name matching** — both first AND last name must match
5. **Always SUPPLIED, never COMPLIANT** — COMPLIANT pulls initial window from years ago
6. **Both 30 AND 90 day reports** when patient has 90+ days of data
7. **Auto-skip:** stale >3mo, data >1yr, Inspire, 0% usage, <30 days
8. **Session management:** check every 5 patients; on expiry, checkpoint + exit (no inline reauth)
9. DME names (Adapt, Aeroflow, Apria, Vie Med) are suppliers, NOT manufacturers
10. **Always generate a run log** at the end of every session
11. **Never call `input()`** in any script — bash_tool is non-interactive
12. **Never paste credentials** into bash commands — use AskUserQuestion + file write

## Reference Files

- `references/schedule_parsing.md` — Mode detection, skip rules, queue building
- `references/airview.md` — ResMed: auth (Okta SSO + MFA), search, download
- `references/care_orchestrator.md` — Philips: auth, search (patientgateway), equipment
- `references/co_reports_api.md` — CO Reports API capture plan
- `references/react_health.md` — 3B: auth, search, report generation
- `references/report_rules.md` — Period logic, filenames, edge cases

## Scripts Directory

- `scripts/utils.py` — Shared: name matching, DOB parsing, recency, encryption, sessions
- `scripts/parse_schedule.py` — PDF → patient_queue.json
- `scripts/auth_co_rh.py` — Auth CO+RH only (no AirView, no inbox side effects)
- `scripts/auth_av.py` — Trigger AirView MFA email
- `scripts/auth_av_verify.py` — Verify MFA + complete OAuth
- `scripts/search_all.py` — Search 3 portals with inline DOB verify, chunked
- `scripts/download_reports.py` — Download PDFs, chunked, checkpoint-on-expiry
- `scripts/gen_spreadsheet.py` — Spreadsheet + log + ZIP
- `scripts/diagnose_co.py` — CO connectivity probe. Run this when `auth_co_rh.py` prints `❌ Failed after 3 attempts` — it walks each hop verbosely, sweeps the 713s clock offset, probes alternate login paths + header shapes, and dumps a shareable capture to `/home/claude/co_diag.json`.

## Known Issues (April 2026)

- **CO report generation:** Real endpoint is `POST /api/documents-v1-0-server/reports/generate`.
  Presigned PDFs land on `cf-s3-e8d69817-a8fa-4178-a98e-90ee9904b805.s3-external-1.amazonaws.com`
  under `/report/{patientUuid}/{objectId}/Compliance Report_{DD-MMM-YYYY}_{HH:MM:SS}.pdf`.
  Exact POST body + auth headers not yet captured. Read `references/co_reports_api.md`
  before marking a CO patient as "manual pull" — it has the existing-report shortcut.
- **AirView validate-then-download:** The current `download_av_report` hits the PDF URL
  directly. The validate endpoint path is not yet confirmed from live traffic — pending
  Walt's capture. Until then, occasional HTML-instead-of-PDF responses are handled by
  the PDF magic-byte check.
- **RH PDF generation:** Auth and search work. PDF requires domPDF approach — not automated.
- **RH pagination:** `utils.auth_co_rh` caches page 1 only. A warning fires if the
  cache is exactly 25 (likely truncated). Full pagination loop pending endpoint capture.
- **AirView SUPPLIED 30-day:** Sometimes returns HTML instead of PDF. Script handles gracefully.
