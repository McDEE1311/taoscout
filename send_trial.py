#!/usr/bin/env python3
"""
TaoScout — manual trial sender.

Usage:
    python3 send_trial.py <email> [days]

Example:
    python3 send_trial.py janschlee@outlook.com 3
"""
import sys

if len(sys.argv) < 2:
    print("Usage: python3 send_trial.py <email> [days]")
    sys.exit(1)

email = sys.argv[1].strip().lower()
days = int(sys.argv[2]) if len(sys.argv) > 2 else 3

from taoscout_auth import create_trial_user

result = create_trial_user(email, trial_days=days, source="admin_cli")

print()
print(f"Status      : {result['status']}")
print(f"Email       : {result['email']}")
print(f"Roster #    : {result['roster_num']}")
print(f"Plan        : {result['plan']}")
if "expires_at" in result:
    print(f"Expires at  : {result['expires_at'][:19]} UTC")
print()
