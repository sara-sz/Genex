#!/usr/bin/env bash
#
# probe_projection_auth.sh — prove the A2 authorization boundary on a REAL
# fictional-staging deployment. NOT run by CI and NOT run by this slice: it
# requires the service to exist, which requires IAM that has not been applied.
#
# WHAT THIS EXISTS TO SETTLE
#
# Cloud Run IAM is the authoritative service-to-service gate. Application-level
# re-verification of the same Google token is OPTIONAL, because it depends on
# what Cloud Run actually delivers to the container — and that is an assumption
# this repository has not yet proven. Probe 5 is what proves or disproves it.
#
# Until probe 5 passes, the service runs PILOT_PROJECTION_AUTH_MODE=iam_only
# and the application must NOT invent an alternate authentication mechanism.
#
# PRECONDITIONS (none applied by this slice)
#   * pilot-projection-staging deployed with --no-allow-unauthenticated
#   * roles/run.invoker granted ONLY to
#       genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com
#   * the operator running this holds Service Account Token Creator on the two
#     service accounts being impersonated, so no key file is needed
#
# FICTIONAL DATA ONLY. Probes 1-4 send no body at all; probe 5 inspects headers.

set -euo pipefail

SERVICE="${SERVICE:-pilot-projection-staging}"
REGION="${REGION:-us-central1}"
PROJECT="${PROJECT:-genex-pilot-staging}"
PARENT_PROJECT="${PARENT_PROJECT:-genex-mvp-2026}"

PARENT_STAGING_SA="genex-parent-staging-run@${PARENT_PROJECT}.iam.gserviceaccount.com"
PARENT_PROD_SA="genex-parent-prod-run@${PARENT_PROJECT}.iam.gserviceaccount.com"
DEFAULT_COMPUTE_SA="1003012205867-compute@developer.gserviceaccount.com"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" \
        --project "$PROJECT" --format='value(status.url)')"
ENDPOINT="${URL}/internal/parent-baseline-projections"
AUDIENCE="$URL"

pass=0; fail=0
check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then printf '  PASS  %-56s (%s)\n' "$1" "$3"; pass=$((pass+1))
  else printf '  FAIL  %-56s expected %s got %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}

token_for() {  # mint an audience-bound ID token for a service account
  gcloud auth print-identity-token \
    --impersonate-service-account="$1" --audiences="$AUDIENCE" 2>/dev/null || true
}

status_of() {  # POST with an optional bearer; print only the status code
  if [ -z "${2:-}" ]; then
    curl -s -o /dev/null -w '%{http_code}' -X POST "$1" \
      -H 'Content-Type: application/json' -d '{}'
  else
    curl -s -o /dev/null -w '%{http_code}' -X POST "$1" \
      -H "Authorization: Bearer $2" -H 'Content-Type: application/json' -d '{}'
  fi
}

echo "=== endpoint: $ENDPOINT"
echo "=== audience: $AUDIENCE"
echo

# -- 0. the service must not be public ---------------------------------------
echo "0. the invoker policy"
members="$(gcloud run services get-iam-policy "$SERVICE" --region "$REGION" \
           --project "$PROJECT" --format=json \
           | python3 -c 'import json,sys; p=json.load(sys.stdin); print(",".join(sorted(m for b in p.get("bindings",[]) if b["role"]=="roles/run.invoker" for m in b.get("members",[]))))')"
check "allUsers is NOT an invoker" "ok" \
      "$([ "${members#*allUsers}" = "$members" ] && echo ok || echo PUBLIC)"
check "the only invoker is the Parent staging SA" \
      "serviceAccount:${PARENT_STAGING_SA}" "$members"
echo

# -- 1. no token -> Cloud Run rejects BEFORE the app -------------------------
echo "1. no token"
check "unauthenticated is rejected by the platform" "403" \
      "$(status_of "$ENDPOINT" "")"
echo

# -- 2. wrong service account -> Cloud Run rejects ---------------------------
echo "2. wrong service identity"
for sa in "$PARENT_PROD_SA" "$DEFAULT_COMPUTE_SA"; do
  tok="$(token_for "$sa")"
  if [ -z "$tok" ]; then
    printf '  SKIP  %-56s (cannot impersonate; that is itself a good sign)\n' "$sa"
  else
    check "rejected: $sa" "403" "$(status_of "$ENDPOINT" "$tok")"
  fi
done
echo

# -- 3. the permitted caller reaches the application -------------------------
echo "3. Parent staging SA + correct audience"
tok="$(token_for "$PARENT_STAGING_SA")"
if [ -z "$tok" ]; then
  echo "  SKIP  could not impersonate $PARENT_STAGING_SA"
else
  # An EMPTY body is deliberate: 400 proves the request reached the
  # application and was refused by the projection validator, which is exactly
  # what "Cloud Run let it through" looks like. A 403 here would mean IAM
  # blocked it; a 200 would mean something accepted an empty projection.
  check "reaches the app (400 from the validator, not 403)" "400" \
        "$(status_of "$ENDPOINT" "$tok")"

  # A token bound to the WRONG audience must still be refused by Cloud Run.
  wrong="$(gcloud auth print-identity-token \
            --impersonate-service-account="$PARENT_STAGING_SA" \
            --audiences="https://example.invalid" 2>/dev/null || true)"
  [ -n "$wrong" ] && check "wrong audience is rejected" "403" \
        "$(status_of "$ENDPOINT" "$wrong")"
fi
echo

# -- 4. (covered by 2) -------------------------------------------------------

# -- 5. THE OPEN QUESTION: what header does the container actually see? ------
echo "5. the header the container receives"
echo "   This is the probe that decides whether iam_plus_token is usable."
echo
echo "   It CANNOT be answered from outside the service: it requires a"
echo "   temporary echo endpoint, or one structured log line, that reports"
echo "   WHICH header carried the token and whether the value still parses as"
echo "   a signed JWT with the expected audience and caller email."
echo
echo "   Run with the service deployed in iam_only, add the echo, and record:"
echo "     * is Authorization present, or only X-Serverless-Authorization?"
echo "     * does the value verify via google.oauth2.id_token"
echo "       .verify_oauth2_token(token, Request(), audience=<service url>)?"
echo "     * does email == ${PARENT_STAGING_SA}?"
echo
echo "   If YES to all three -> PILOT_PROJECTION_AUTH_MODE=iam_plus_token is"
echo "   proven and may be enabled."
echo "   If NO -> Cloud Run IAM REMAINS AUTHORITATIVE, the mode stays"
echo "   iam_only, and the application must NOT invent an alternate"
echo "   authentication mechanism. No shared secret, no static key, no"
echo "   custom token."
echo
echo "   The echo endpoint must never log the token itself — only which"
echo "   header name carried it, and the boolean verification outcomes."
echo

printf '=== probes 0-3: %d passed, %d failed; probe 5 is manual\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
