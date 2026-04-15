# Report Rules Reference

## Pre-Download Verification

**Rule #1: Verify DOB before every download.**
A name match is NOT sufficient. Open the patient profile and confirm DOB matches before
pulling any report. This rule exists because wrong-patient reports were downloaded in
production when DOB was not checked. If DOB cannot be verified (not in search results,
not on schedule), flag for user review — do not download.

## Report Types by Platform

| Platform | Report Name | Notes |
|---|---|---|
| AirView (ResMed) | Compliance and Therapy | URL says "compliance" but includes therapy data |
| Care Orchestrator (Philips) | Sleep Trend | Different report type from compliance |
| React Health Connect | Compliance and Therapy | Same name as AirView, different platform |

## Report Period Logic

| Available data | Action |
|---|---|
| < 30 days | Skip — "insufficient data (X days)" |
| 30–89 days | Pull **30-day report only** |
| 90+ days | Pull **both 30-day AND 90-day reports** |

Always download both when 90+ days available. Provider needs the right report regardless of visit type.

## Filename Conventions

Use the patient name **as it appears in the manufacturer portal** (preserves correct spelling/capitalization).

| Period | Format | Example |
|---|---|---|
| 30-day | `{PortalName}.pdf` | `WARD, LESHIKA.pdf` |
| 90-day | `{PortalName} 90.pdf` | `WARD, LESHIKA 90.pdf` |

## Auto-Skip Rules (apply before downloading)

These are NOT ambiguous. Skip automatically, report in summary:

- **Stale data (>3 months since last sync):** Most recent profile hasn't synced in 3+ months. Patient isn't actively using the device.
- **Data over 1 year old:** Tag as **"Bring device - data over X old"** on the spreadsheet. Do NOT download.
- **0% Last 30 on ALL profiles:** Patient exists but isn't using any device.
- **Insufficient data (<30 days):** Not enough for a meaningful report.
- **Inspire patients:** Auto-skip. Notes will say "Inspire" in the procedure. These patients do not use PAP devices — Inspire is a surgically implanted nerve stimulator. Never download reports for Inspire patients.

## Flag for Review (genuinely ambiguous only)

- **Ambiguous visit type:** Could be Inspire, not PAP. No reliable note pattern for Inspire — user knows.
- **Multiple active profiles with similar recency:** Not obvious which is the right device.
- **Loose name match:** Portal name doesn't closely match Epic name.

## DME vs Manufacturer

Notes often include DME supplier names in parentheses. These are NOT manufacturers:
- **DME suppliers:** Adapt, Adapt Health, Aeroflow, Apria, Vie Med, Nationwide
- **Manufacturer markers (post-download only):** `C AV` = AirView/ResMed, `3B` = React Health, `PH` = Philips/CO

The markers are written AFTER the download is complete. For patients needing reports,
there is no manufacturer hint — search all three portals.

## Edge Cases

| Situation | Action |
|---|---|
| Multiple profiles per patient | Sort by "Last updated" recency, try each in order |
| Stale profile (>3 months) | Auto-skip, no flag |
| Possible Inspire patient | Flag — user knows who they are |
| Hyphenated last name | Search first part, verify full name in results |
| Nickname in quotes ("Billy") | Strip quotes for search, use portal spelling for filename |
| Jr./Sr./II/III suffix | Include in search, match flexibly |
| SUPPLIED 30-day returns empty | Still pull the 90-day — SUPPLIED 30 sometimes fails when 90 works |
| Wrong name match in search results | DO NOT download. Different first name = different patient = different manufacturer |
| Session expires mid-batch | Re-authenticate (new MFA code needed for AirView) |
| AirView returns 39-byte HTML | Proxy issue — confirm using `requests`, not Selenium |
| MFA code rejected | Code consumed by re-trigger — ask for newest code |

## Rate Limiting

`time.sleep(0.8)` between patient requests on all platforms.
