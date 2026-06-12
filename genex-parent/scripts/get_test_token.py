#!/usr/bin/env python3
"""
scripts/get_test_token.py — Get a real Firebase ID token for smoke testing.

Uses the same Firebase Identity Platform REST endpoint that the Streamlit
app uses for sign-in. The token is valid for ~1 hour and can be used as
the Bearer token in deployed API smoke tests.

Usage:
    python3 scripts/get_test_token.py

Reads from env vars (or prompts if not set):
    FIREBASE_API_KEY   — from Secret Manager (same key used by the Streamlit app)
    TEST_EMAIL         — email to sign in with (default: soltanizadehsara@protonmail.com)
    TEST_PASSWORD      — password (prompted securely if not set)

Outputs:
    export GENEX_API_TOKEN=<id_token>

The token is printed as a shell export command so you can eval it:
    eval $(python3 scripts/get_test_token.py)
"""

import getpass
import json
import os
import sys
import urllib.request
import urllib.error

FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "").strip()
TEST_EMAIL       = os.environ.get("TEST_EMAIL", "soltanizadehsara@protonmail.com").strip()
TEST_PASSWORD    = os.environ.get("TEST_PASSWORD", "").strip()

if not FIREBASE_API_KEY:
    print("ERROR: FIREBASE_API_KEY env var not set.", file=sys.stderr)
    print("       Get it from: gcloud secrets versions access latest --secret=FIREBASE_API_KEY", file=sys.stderr)
    sys.exit(1)

if not TEST_PASSWORD:
    TEST_PASSWORD = getpass.getpass(f"Password for {TEST_EMAIL}: ")

url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
payload = json.dumps({
    "email":             TEST_EMAIL,
    "password":          TEST_PASSWORD,
    "returnSecureToken": True,
}).encode("utf-8")

req = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8")
    print(f"ERROR: Firebase sign-in failed ({exc.code}): {body}", file=sys.stderr)
    sys.exit(1)

id_token = data.get("idToken", "")
if not id_token:
    print(f"ERROR: No idToken in response: {data}", file=sys.stderr)
    sys.exit(1)

email_out    = data.get("email", "")
local_id_out = data.get("localId", "")
expires_in   = data.get("expiresIn", "3600")

print(f"# Signed in as: {email_out} (uid: {local_id_out})")
print(f"# Token expires in: {expires_in}s (~{int(expires_in)//60} minutes)")
print(f"export GENEX_API_TOKEN={id_token}")
