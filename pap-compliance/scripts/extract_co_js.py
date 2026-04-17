#!/usr/bin/env python3
"""
Extract the reports/generate request body shape from CO's Angular JS bundles.
The frontend source code contains the exact HTTP call — no browser HAR needed.
"""
import json, sys, os, re, base64, requests
from datetime import datetime, timedelta, timezone

CREDS_PATH = os.environ.get("CREDS_PATH", "/tmp/pap/pap_creds.json")
CO_BASE = "https://www.careorchestrator.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
OUT = os.environ.get("OUT", "/tmp/pap/co_js_extract.json")

report = {"steps": []}

def log(step, data):
    print(f"\n== {step} ==", flush=True)
    if isinstance(data, str):
        print(f"  {data[:500]}", flush=True)
    elif isinstance(data, dict):
        for k, v in data.items():
            vs = str(v)
            if len(vs) > 400: vs = vs[:400] + "..."
            print(f"  {k}: {vs}", flush=True)
    report["steps"].append({"step": step, "data": data})

s = requests.Session()
s.headers.update({"User-Agent": UA})

# 1. Fetch the CO index page to find JS bundle URLs
r = s.get(f"{CO_BASE}/", timeout=30)
log("1. index page", {"status": r.status_code, "size": len(r.text)})

# Find all script src URLs
scripts = re.findall(r'src="([^"]*\.js[^"]*)"', r.text)
# Also check for lazy-loaded chunks referenced in the HTML
scripts += re.findall(r"src='([^']*\.js[^']*)'", r.text)
log("2. script tags found", {"count": len(scripts), "urls": scripts})

# Also check if there's an app config with bundle info
config_scripts = re.findall(r'(?:href|src)="([^"]*(?:main|app|vendor|chunk|bundle)[^"]*)"', r.text, re.IGNORECASE)
log("2a. main/app/vendor scripts", config_scripts)

# 2. Download each JS bundle and search for reports/generate
matches_found = []

for script_url in scripts:
    if script_url.startswith("/"):
        full_url = f"{CO_BASE}{script_url}"
    elif script_url.startswith("http"):
        full_url = script_url
    else:
        full_url = f"{CO_BASE}/{script_url}"

    try:
        jr = s.get(full_url, timeout=30)
        if jr.status_code != 200:
            continue

        js = jr.text
        log(f"3. fetched {script_url}", {"size": len(js)})

        # Search for reports/generate references
        patterns = [
            r'reports/generate',
            r'documents-v1-0-server',
            r'generateReport',
            r'reportGenerat',
            r'sleepTrend',
            r'Sleep.?Trend',
            r'templateId',
            r'Compliance.?Report',
        ]

        for pat in patterns:
            for m in re.finditer(pat, js, re.IGNORECASE):
                start = max(0, m.start() - 300)
                end = min(len(js), m.end() + 300)
                context = js[start:end]
                # Clean up for readability
                context = context.replace('\n', ' ').strip()
                match_info = {
                    "pattern": pat,
                    "file": script_url,
                    "position": m.start(),
                    "context_600_chars": context,
                }
                matches_found.append(match_info)
                log(f"4. MATCH [{pat}] in {script_url} @ {m.start()}", context)

    except Exception as e:
        log(f"3. FAILED {script_url}", {"error": str(e)})

# 3. Also try fetching the encrypted app config and look for service definitions
try:
    cr = s.get(f"{CO_BASE}/app/config", timeout=15)
    log("5. /app/config", {"status": cr.status_code, "size": len(cr.text),
                            "snippet": cr.text[:500]})
except Exception as e:
    log("5. /app/config", {"error": str(e)})

# 4. Try common Angular chunk patterns
chunk_patterns = [
    "/main.js", "/main.*.js", "/app.js", "/app.*.js",
    "/scripts.js", "/polyfills.js", "/runtime.js",
    "/vendor.js", "/chunk-*.js",
]
# Try to find chunk manifest or webpack stats
for extra_path in ["/ngsw.json", "/assets/config.json", "/webpack-stats.json",
                    "/asset-manifest.json", "/manifest.json"]:
    try:
        mr = s.get(f"{CO_BASE}{extra_path}", timeout=10)
        if mr.status_code == 200 and len(mr.text) > 10:
            log(f"6. {extra_path}", {"status": mr.status_code,
                                      "snippet": mr.text[:500]})
            # Extract any JS URLs from manifests
            more_scripts = re.findall(r'"([^"]*\.js[^"]*)"', mr.text)
            if more_scripts:
                log(f"6a. additional scripts from {extra_path}",
                    {"count": len(more_scripts), "urls": more_scripts[:20]})
                scripts.extend(more_scripts)
    except:
        pass

# 5. Summary
log("7. SUMMARY", {
    "total_matches": len(matches_found),
    "unique_patterns": list(set(m["pattern"] for m in matches_found)),
    "files_with_matches": list(set(m["file"] for m in matches_found)),
})

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n{'='*50}")
print(f"DONE. {len(matches_found)} matches found across {len(scripts)} JS bundles.")
print(f"Full capture at {OUT}")
print("Return ALL terminal output AND the full contents of that file.")
print("Most important: the 600-char context around any 'reports/generate' match —")
print("that's the function that builds the POST body.")
