# Schedule Parsing Reference

## Mode Detection

**Batch (DAR format):** Multi-day schedule, minimal columns: `Visit Date | MRN | Time | Arrival Status | Patient | Type | Appt Notes | Provider`. Dates span 2+ weeks.

**Reconciliation (daily schedule):** Single-day clinic printout, detailed format: `Time | Patient | Age/Gender | Status | Notes/Procedure | Provider | PCP | Department`. Has full procedure notes, phone numbers, provider details. Much more "bullshit" to parse.

## Skip Rules

Apply these in order. If any match, skip the patient.

### Already Handled
- Notes end with `- WS` → signed off, report already pulled
- Notes contain `DL in chart` with a date → already downloaded
- Notes contain `DL IN chart` (case variations) → already downloaded

### Not PAP
- Visit type = `New Patient` with NP-style notes ("NP refd by", "NP, referred", "NP self ref") → no PAP data
- Notes contain: "NOT USING", "NO PAP", "Medication"
- Notes say "LVM to switch" → switching devices/appointments
- Notes say "no data on device" → nothing to pull remotely

### Separate Workflow
- Provider = `TECH_SDC_MONUMENT` → tech workflow
- Visit type = `Remote Download` → tech workflow
- Visit type = `Watchpat` → sleep study device pickup/return, not PAP
- Notes say "WatchPAT" or "Watchpat P/U" → sleep study, not PAP
- Notes say "DL RESMED DR" with provider TECH_SDC_MONUMENT → tech download

### Not PAP / Inspire
- Notes say "Inspire" → Inspire patient (surgically implanted nerve stimulator, NOT PAP). Always skip.
- Visit type = `Implant Follow Up` with Inspire in notes → Inspire. Always skip.

### Device Not Downloadable
- Notes say "Device not downloadable" → patient bringing physical machine, can't pull from portal

## Queue Building

Everything not skipped goes into the queue. For reconciliation mode, the queue is typically
much smaller (3-6 patients) vs batch (50-100+).

## Output After Parse

```
Parsed X patients. Y queued for reports, Z skipped.
[list of queued patients with their notes]
[list of skipped with reason categories]
```

Wait for user confirmation before authenticating.
