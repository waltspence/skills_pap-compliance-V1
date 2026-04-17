# pap-compliance patch — apply instructions

Audit completed April 10, 2026. This patch fixes the breaking bugs and doc gaps
found in v2. `/mnt/skills/` is read-only at runtime, so apply these changes
between sessions (or have a fresh Claude session copy them into the skill dir
during initial setup from `/mnt/user-data/uploads/`).

## Files to REPLACE in `/mnt/skills/user/pap-compliance/`

| Source (in this zip)          | Destination                                            | Why |
|-------------------------------|--------------------------------------------------------|-----|
| `SKILL.md`                    | `pap-compliance/SKILL.md`                              | R4/S2/S3/S4/S5 |
| `scripts/search_all.py`       | `pap-compliance/scripts/search_all.py`                 | B1, B3 |
| `scripts/download_reports.py` | `pap-compliance/scripts/download_reports.py`           | B2, B3 |
| `scripts/auth_co_rh.py`       | `pap-compliance/scripts/auth_co_rh.py`                 | R3 |
| `scripts/utils.py`            | `pap-compliance/scripts/utils.py`                      | S1, R6 |

## Files to DELETE from `/mnt/skills/user/pap-compliance/scripts/`

- `pap_compliance_scripts.zip` — stale artifact, R5

## What changed, in one line each

**B1** `search_all.py`: deleted lines 384–516 (duplicated module-scope copy of main that ran searches 2–3× on every invocation).
**B2** `download_reports.py`: same — deleted duplicated tail, kept one `main()` + one `if __name__` guard.
**B3** Both scripts: `reauth_inline()` → `checkpoint_and_exit()`. No more `input()` calls (bash_tool is non-interactive and would hang). On AV expiry, scripts now save state and `sys.exit(2)` with resume instructions. Does NOT auto-trigger MFA from inside a worker — user re-auths between chunks.
**R3** `auth_co_rh.py`: removed the `os.system("pip install ...")` line. Install is now a one-time Phase 0 step in SKILL.md.
**R4** SKILL.md: removed both `auth_all.py` references (file doesn't exist, doc was stale).
**R5** Delete `scripts/pap_compliance_scripts.zip` — stale artifact, not referenced anywhere.
**R6** `utils.py` `auth_co_rh()`: prints a ⚠️ warning if `len(rh_patients) == 25` — likely pagination truncation. Safety net only; full pagination pending endpoint capture.
**S1** `utils.py` `co_secret_key()`: added docstring explaining the 713s offset derivation, added `CO_CLOCK_OFFSET` env var override. Default 713 preserved as fallback.
**S2** SKILL.md: documented `--offset 0 --limit 20` chunking pattern explicitly for Phase 3 and Phase 4, with loop examples.
**S3** SKILL.md: replaced inline `cat > heredoc` credential block with AskUserQuestion guidance. Kept the expected JSON shape so the field names are visible.
**S4** SKILL.md: added top-of-file "Editing this skill" section noting `/mnt/skills/` is read-only at runtime and fixes must be emitted as output prompts.
**S5** SKILL.md: specified exact final ZIP path (`/mnt/user-data/outputs/PAP_Compliance_{YYYY-MM-DD}.zip`), its contents (xlsx, reports/*.pdf, run_log.json, download_log.json, search_results.json), and Phase 6 `present_files` call.

## What was NOT changed (per Walt's instructions)

- **R1 validate-then-download**: held. Validate endpoint path is inferred, not confirmed from live traffic. Walt will capture a sample and bring it back.
- **R6 full pagination fix**: only the warning was added. Full loop pending endpoint capture (same reason as R1).

## Validation performed in the patch session

All four modified Python files parse cleanly (`ast.parse`). No `input(` calls remain
outside of comment strings. Each of `search_all.py` and `download_reports.py` has
exactly one `def main` and one `if __name__ == "__main__"` guard.

## Post-apply smoke test

```bash
cd /mnt/skills/user/pap-compliance
python -c "import ast; [ast.parse(open(f).read()) for f in ['scripts/search_all.py','scripts/download_reports.py','scripts/utils.py','scripts/auth_co_rh.py']]; print('OK')"
python scripts/auth_co_rh.py   # should print CO ✅, RH ✅ (no pip install noise)
```

## CO-connect root cause (April 2026, live test on repo landing)

Ran `python scripts/auth_co_rh.py` with stub creds. It failed with:
`❌ Failed after 3 attempts: name 'os' is not defined`.

Cause: the S1 patch to `utils.co_secret_key()` added `os.environ.get("CO_CLOCK_OFFSET", "713")`
but `utils.py` does not `import os` at the top — every other `os` use in the file is
aliased inline as `_os` for session dir handling. `co_secret_key()` runs on every CO
login attempt (and therefore every session-health check), so every CO connect has been
dying at the key-derivation step before any network call is made.

The symptom — "CO stopped connecting" across 3 retries with no HTTP status printed —
looks identical to a backend change. Nothing on Philips' side actually moved for auth;
this is a client-side regression from the R6/S1 patch. The S5-era doc note that
reports API moved to `documents-v1-0-server` is still real, but unrelated to this bug.

**Fix:** `import os` added at top of `utils.py`.

**Also applied in the same pass (defense in depth):**
- `_co_login` now raises `ValueError("HTTP {status} at {url} — body[:300]={…}")` on
  4xx/5xx or non-JSON bodies instead of letting `r.json()` throw a JSONDecodeError.
  Makes retry-loop failure messages tell you the real problem next time.
- `auth_co_rh` retry loop now fails fast on `NameError`, `AttributeError`, `TypeError`,
  `KeyError`, `ImportError` — these are coding/config bugs that can't succeed on
  retry, so retrying wastes ~9s and hides the real exception type. The error message
  also now includes the exception class name (`ValueError: ...`, `NameError: ...`)
  so the bug class is visible at a glance instead of buried in a generic wrapper.
  Verified: injected NameError fails in 0.13s with "Failed after 1 attempt:
  NameError: ... (client-side bug, not retried)".
- Added `scripts/diagnose_co.py` — a verbose probe that walks each CO step, sweeps
  `CO_CLOCK_OFFSET`, tries alternate login paths and header shapes, and dumps a
  shareable capture at `/home/claude/co_diag.json`. See the SKILL.md scripts list.
- `references/care_orchestrator.md` reconciled with `co_reports_api.md`: the old
  "reports gen permanently broken" language was a wrong turn; the live path is
  `POST /api/documents-v1-0-server/reports/generate`.

## CO Sleep Trend download — first automated attempt (April 2026)

Implemented the speculative CO download path so we stop waiting for a browser
DevTools capture from Walt.

- `utils.download_co_sleep_trend(co_session, co_headers, patient_uuid, serial)`:
  builds the best-guess POST body (shape borrowed from the old
  therapyreporttemplates body in care_orchestrator.md), tries both
  `/api/documents-v1-0-server/reports/generate` and
  `/proxy/documents-v1-0-server/reports/generate`, and handles three response
  shapes: direct PDF, JSON with a `presignedUrl`, or JSON with a `documentId`
  that then gets looked up via `/proxy/documents-v1-0-server/reports/presigned`.
  On any failure, appends the full response to `/home/claude/co_generate_capture.json`
  so the next iteration has status + headers + body[:500] to work from.
- `utils.co_get_equipment_serial(...)`: resolves the primary device serial
  via `equipment-v1-0-server`.
- `download_reports.py`: runs a CO pass after the AV pass, gated on
  co_session.pkl presence. Per-patient lines print OK + method, or FAIL +
  pointer to the capture file. The chunk-complete summary now shows
  AV successes/failures, CO successes/failures, and total PDFs on disk.
- `diagnose_co.py`: added steps 17-19 — picks a real patient from the
  wildcard search, resolves their serial, and POSTs the Sleep Trend generate
  body to both route variants. Whatever the server says on the first live
  run is the unlock.
- `SKILL.md` Phase 4: "NOT YET AUTOMATED" replaced with the real flow; the
  platforms-table status for CO is now 🟡 (automated, speculative) instead
  of ❌ (pending capture).

Smoke-tested with mocked sessions:
- Direct PDF: saved, returns OK, method=direct_pdf.
- JSON presignedUrl: follow-up GET, PDF saved, method=presigned.
- 400 then 500 on both routes: FAIL, capture file has full bodies for triage.

Still unknown until a live run:
- Whether the POST body shape is accepted or rejected (and by which field).
- Whether the response is direct PDF or async with a documentId to poll.
- Whether any extra headers (CSRF, X-XSRF-TOKEN, JWT bearer) are required.

Whatever it is, the first live run with real creds produces either a PDF or
`/home/claude/co_generate_capture.json` that pinpoints what needs to change.

## Round 2 hardening (April 2026, repo landing)

After the CO-connect root cause was fixed, did a systematic pass for the same class
of bug across the other auth paths and the download pipeline:

- **AV `authn` and MFA verify** now raise `ValueError("HTTP {status} at {url} — body[:300]={…}")`
  on 4xx/5xx or non-JSON responses, same pattern as CO `_co_login`. Previously,
  Okta returning HTML (429 / anti-bot / rate limit) would surface as
  `JSONDecodeError: Expecting value: line 1 column 1` — unactionable.
- **AV no-email-MFA case** now raises a clear error listing which factors ARE enrolled
  on the account, instead of an opaque `IndexError` from `[...][0]` on an empty list.
- **CO `sessions/context` POST** is now checked. A silent 401/403 there left the
  session on the default org and made patientgateway searches silently return wrong-org
  or empty data — looked like "the search works but finds nothing." Now raises with
  status + body + orgId so the retry loop surfaces it.
- **`load_creds`** raises a descriptive `FileNotFoundError` pointing at the expected
  path and reminding the caller to use `AskUserQuestion`, not a bash heredoc.
- **`download_av_report`** captures up to 200 chars of the response body on FAIL so
  "HTML instead of PDF" failures (the AirView SUPPLIED 30-day edge case noted in the
  Known Issues) can be triaged without a network capture.
- **`download_reports.py` chunk summary** now actually prints the counts it was already
  tracking: patients with ≥1 successful PDF, patients with failures only, total PDFs
  on disk with size. Previously computed `pdf_count` and `total_size` in dead code.

Verified locally:
- `load_creds()` with missing file: descriptive error, no stack.
- `auth_co_rh` with mocked 401 on `sessions/context`: 3 retries, each failing with
  `ValueError: sessions/context HTTP 401 — body[:200]='{"error":"unauthorized"}' (orgId='org-1')`.
- `auth_co_rh` with injected `NameError`: still fails fast in 0.12s (regression test).
- All 9 scripts parse cleanly.

## Follow-up cleanup (April 2026, repo landing)

Applied on top of the v3 patch above while moving the skill into version control:

- **gen_spreadsheet.py**: removed leftover `os.system("pip install openpyxl ...")`. `openpyxl` is covered by Phase 0, consistent with the R3 pattern.
- **parse_schedule.py**: removed the eager `install_deps()` helper. `pdfplumber` is in Phase 0; `pdf2image` / `pytesseract` remain lazy imports inside `parse_dar_ocr()` since OCR also needs `poppler-utils` + `tesseract-ocr` system binaries.
- **SKILL.md Phase 0**: added an OCR-fallback install block (`apt-get install poppler-utils tesseract-ocr` + `pip install pdf2image pytesseract`) so the OCR path is documented even though the common case doesn't need it.
