# Care Orchestrator Reports API — Intel & Capture Plan

Status: **partially reverse-engineered, not yet automated.** Report generation is NOT
permanently broken. Earlier "502s" came from hitting the wrong service
(`sapphiregateway`). The real service is `documents-v1-0-server`. This file tracks
what's confirmed, what's still needed, and how to capture the rest.

**Canonical target report: Sleep Trend.** Template id
`ebedbf1a-be12-4756-9661-85dc7bec1792` (per the templates list in
`references/care_orchestrator.md`). Use **Trilogy Detail** only for ventilator
patients. "Compliance Report" appears in the UI dropdown and in earlier notes, but
Sleep Trend is the per-site standing SOP — generate that one.

## Confirmed (from 2026-04 browser session on patient Stephen Moore)

**Auth / login:** Unchanged. Use `scripts/auth_co_rh.py` (wspence creds). CO session
cookie carries over to the documents API on the same host.

**Patient URL pattern:**
```
https://www.careorchestrator.com/#/patient/{patientUuid}/therapydata/reports
```
`{patientUuid}` is the CO patient UUID already returned by the patientgateway search
(e.g. Stephen Moore → `15a98a68-d7f0-474f-b6ff-3a7d069f9af6`). Reuse it — do not
re-resolve.

**Report generation endpoint (confirmed April 2026 live probe):**
```
POST https://www.careorchestrator.com/proxy/documents-v1-0-server/reports/generate
```
The `/api/` variant (`/api/documents-v1-0-server/reports/generate`) returns 404
"Cannot POST" — that route does not exist on the backend. Only `/proxy/` is live.

Response on 400: `Content-Type: application/octet-stream`, empty body. This means
the endpoint returns binary data (likely a direct PDF) on success, and an empty
400 when the body is wrong — no JSON error, no field-level rejection message.

Auth header: confirmed `auth_token: <JSON stringified token>` only. `Bearer` and
`X-Auth-Token` variants both 401.

Body shape: NOT yet confirmed. The body from the old `therapyreporttemplates`
endpoint (templateId, patientId, deviceSerialNumber, startDate, endDate, etc.)
returns 400. The skill now tries 6 body variants on each attempt and captures all
responses — see `scripts/utils.py _co_generate_body_variants()`.

**Report templates visible in the dropdown:**
- Compliance Report
- Compliance Summary
- Trilogy Detail (ventilator patients)

The UI dropdown labels don't match the templates API 1:1 — the templates list in
`care_orchestrator.md` returns `Trend (Sleep Trend)`, `ComplianceSummary`, `Detail`,
`Summary`, `Patient`. **Our standing target is Sleep Trend** (API template id
`ebedbf1a-be12-4756-9661-85dc7bec1792`). When capturing the `reports/generate` POST,
generate the **Sleep Trend** report, not Compliance Report, so the body we record is
the right one. Use **Trilogy Detail** only for Trilogy vent patients.

**Default timeframe:** UI pre-populates 30 days. Match our standard (30 day, and a
90-day if patient has ≥90 days data — same rule as AirView).

**Presigned PDF delivery (confirmed working — this is the big one):**
Generated PDFs land in S3:
```
Host:   cf-s3-e8d69817-a8fa-4178-a98e-90ee9904b805.s3-external-1.amazonaws.com
Region: us-east-1
Key:    /report/{patientUuid}/{objectId}/Compliance Report_{DD-MMM-YYYY}_{HH:MM:SS}.pdf
```
Example key observed:
`/report/15a98a68-d7f0-474f-b6ff-3a7d069f9af6/bc60138b-6528-45c1-89e2-a3f6506f85bf/Compliance%20Report_09-Mar-2026_21%3A25%3A33.pdf`

The PDF renders normally (5 pages, real ventilation data). So once we have the
presigned URL, download is a plain `requests.get()`.

## Still needed to fully automate

1. **Exact `/reports/generate` POST body** — presumably `{templateId, patientId,
   startDate, endDate, ...}` but field names unknown.
2. **Required headers** — likely `Authorization: Bearer <jwt>` or a CSRF/XSRF header
   in addition to the session cookie. Check request headers when captured.
3. **The polling/presigned-URL step** — generate probably returns a job id, and a
   second call (something like `/reports/{id}/presigned` or `/reports/status/{id}`)
   returns the S3 URL. This second endpoint name is not yet confirmed.

## Capture plan (do this in a real browser, not automation)

The Comet automation env can't reach DevTools — `javascript:` URLs get search-boxed,
DevTools renders outside the screenshot viewport, and `Ctrl+` ` is eaten by the Angular
app. So capture must happen on Walt's actual workstation:

1. Chrome → `https://www.careorchestrator.com`, login as `wspence` / `@HdmDx5&nEHL`.
2. Navigate to `/#/patient/{anyPatientUuid}/therapydata/reports`.
3. F12 → Console, paste fetch interceptor:
   ```js
   (function(){
     const o=window.fetch; window.__cap=[];
     window.fetch=function(...a){
       const [u,opt]=a;
       const match=String(u).includes('/reports/');
       if(match) window.__cap.push({url:String(u),method:opt?.method||'GET',headers:opt?.headers||{},body:opt?.body||null,ts:Date.now()});
       return o.apply(this,a).then(async r=>{
         if(match){ try{ const t=await r.clone().text();
           const rec=window.__cap[window.__cap.length-1]; rec.status=r.status; rec.response=t; }catch(e){} }
         return r;
       });
     };
     return 'installed';
   })();
   ```
4. Network tab → filter `reports` → Clear.
5. In the UI: template = **Sleep Trend** (the Philips dropdown may label it "Trend" or
   "Sleep Trend"), range = 30 days, click **Create report**. DO NOT pick "Compliance
   Report" — the captured body will be for the wrong template and won't help us.
6. In Network: right-click the `/reports/generate` POST → Copy → Copy as cURL (bash).
   Do the same for any follow-up call (status poll, presigned URL fetch).
7. Console: `copy(JSON.stringify(window.__cap, null, 2))` → paste into a new chat as
   `co_reports_capture.json`.

Once that JSON lands, updating `scripts/download_reports.py` to handle CO is a
straightforward port — the hard parts (host, route, S3 layout, patient UUID reuse)
are already known.

## Shortcut worth trying first: reuse the most recent existing report

The reports list on `/therapydata/reports` shows prior generations for that patient.
Clicking an existing row resolves to the same S3 presigned URL — no regeneration
needed. If the clinic only needs "a recent compliance report" (not specifically
today's run), we can:

1. Hit the list endpoint for the patient (also under `documents-v1-0-server`, route
   TBD — capture alongside the generate call).
2. Pick the newest matching template within the requested window.
3. Download it directly from S3.

This avoids the generate call entirely for patients who already had a report pulled
within the last N days, which is common on repeat visits. Implement this path first
once the list endpoint is captured — it's lower-risk than driving generation.

## Do NOT

- Do not fall back to sapphiregateway. That was the original wrong turn.
- Do not hardcode `objectId` — it's generated per report.
- Do not assume the S3 host is stable long-term; it's CloudFront-fronted and the
  bucket id could rotate. Always parse the host from the presigned response.
- Do not re-resolve the patient UUID — reuse the one from patientgateway search.
