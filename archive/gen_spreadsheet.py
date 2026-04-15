#!/usr/bin/env python3
"""
pap-compliance/scripts/auth_av_verify.py
Verify AirView MFA code and complete OAuth.
Usage: python scripts/auth_av_verify.py <MFA_CODE>
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from utils import auth_av_verify

if len(sys.argv) < 2:
    print("Usage: python scripts/auth_av_verify.py <MFA_CODE>")
    sys.exit(1)

try:
    auth_av_verify(sys.argv[1].strip())
    print("AV: ✅ Session LIVE")
except Exception as e:
    print(f"AV: ❌ {e}")
    sys.exit(1)
