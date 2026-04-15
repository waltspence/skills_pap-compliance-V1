#!/usr/bin/env python3
"""
pap-compliance/scripts/auth_av.py
Trigger AirView MFA email. Run when ready to enter the code.
After running: python scripts/auth_av_verify.py <CODE>
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from utils import load_creds, auth_av_trigger

creds = load_creds()
try:
    _, state_msg = auth_av_trigger(creds)
    print(f"  AV: ✅ MFA sent to {creds['AirView']['email']} ({state_msg})")
    print("\nPaste the AirView MFA code, then run: python scripts/auth_av_verify.py <CODE>")
except Exception as e:
    print(f"  AV: ❌ {e}")
