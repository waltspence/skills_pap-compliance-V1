#!/usr/bin/env python3
"""
pap-compliance/scripts/auth_co_rh.py
Authenticate CO and RH. No AirView — run auth_av.py separately when ready for MFA.

Dependencies (pycryptodome, beautifulsoup4) are installed once in Phase 0 of SKILL.md.
Do NOT pip install here — it eats the bash_tool budget on every chunked call.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from utils import load_creds, auth_co_rh

creds = load_creds()
results = auth_co_rh(creds)

print("=" * 50)
for k, v in results.items():
    print(f"  {k}: {v}")
print("=" * 50)
print("\nRun next: python scripts/auth_av.py  (triggers AirView MFA email)")
