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
- Added `scripts/diagnose_co.py` — a verbose probe that walks each CO step, sweeps
  `CO_CLOCK_OFFSET`, tries alternate login paths and header shapes, and dumps a
  shareable capture at `/home/claude/co_diag.json`. See the SKILL.md scripts list.
- `references/care_orchestrator.md` reconciled with `co_reports_api.md`: the old
  "reports gen permanently broken" language was a wrong turn; the live path is
  `POST /api/documents-v1-0-server/reports/generate`.

## Follow-up cleanup (April 2026, repo landing)

Applied on top of the v3 patch above while moving the skill into version control:

- **gen_spreadsheet.py**: removed leftover `os.system("pip install openpyxl ...")`. `openpyxl` is covered by Phase 0, consistent with the R3 pattern.
- **parse_schedule.py**: removed the eager `install_deps()` helper. `pdfplumber` is in Phase 0; `pdf2image` / `pytesseract` remain lazy imports inside `parse_dar_ocr()` since OCR also needs `poppler-utils` + `tesseract-ocr` system binaries.
- **SKILL.md Phase 0**: added an OCR-fallback install block (`apt-get install poppler-utils tesseract-ocr` + `pip install pdf2image pytesseract`) so the OCR path is documented even though the common case doesn't need it.
