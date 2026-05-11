# Genex Alpha — Cloud Run Deployment Guide

**Project:** genex-mvp-2026  
**Account:** sara@getgenex.com  
**Service:** genex-alpha  
**Region:** us-central1  
**Image registry:** Artifact Registry (not gcr.io / Container Registry)  
**GCS bucket:** genex-alpha-sessions-genex-mvp-2026  

---

## Architecture decisions

### Container registry — Artifact Registry, not gcr.io
Container Registry (gcr.io) is deprecated by Google. Artifact Registry is the current
standard and will continue to receive updates and support. All image URLs in this guide
use the Artifact Registry format:
`us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest`

### Cloud Storage — added, sessions and feedback persist
Session JSON and advisor feedback write to GCS (`genex-alpha-sessions-genex-mvp-2026`).
Cloud Run containers are ephemeral — they restart, scale to zero, and lose `/tmp/` on
every cold start. GCS is the only reliable store at alpha scale. Already wired in
`genex_core/storage.py`.

### Cloud SQL — not yet
Cloud SQL adds a VPC connector, a schema, migrations, and connection pooling. Not needed
while data fits in GCS JSON files. Revisit at Beta when you need cross-session queries,
advisor dashboards, or usage analytics.

### OpenAI API key — add separately after first deploy
The key is intentionally not in the deploy command below. After the first successful
deploy (even without AI — the app runs on deterministic fallbacks), add it via
Secret Manager (instructions at the bottom of this file).

---

## Phase summary

| Phase | Who can access | Setup time | What it requires |
|---|---|---|---|
| Phase 1 | Sara only | ~30 min | gcloud CLI on Sara's Mac |
| Phase 2A | Advisors with Google accounts | ~60 min extra | Domain, DNS, Load Balancer, IAP |
| Phase 2B | Advisors with any browser | ~15 min extra | Shared password in app (temporary) |

---

## Prerequisites (run once)

```bash
# Confirm you are logged in as sara@getgenex.com
gcloud auth login
gcloud config set project genex-mvp-2026

# Enable all required APIs (takes ~2 min the first time)
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  iap.googleapis.com \
  compute.googleapis.com \
  secretmanager.googleapis.com \
  --project genex-mvp-2026
```

---

## Step 1 — Create the Artifact Registry repository

This is a one-time setup step. The repository holds all future image versions.

```bash
gcloud artifacts repositories create genex-alpha \
  --repository-format=docker \
  --location=us-central1 \
  --description="Genex Alpha container images" \
  --project genex-mvp-2026

# Verify it was created
gcloud artifacts repositories list \
  --location=us-central1 \
  --project genex-mvp-2026
```

---

## Step 2 — Create the GCS bucket

```bash
gsutil mb \
  -p genex-mvp-2026 \
  -l us-central1 \
  -b on \
  gs://genex-alpha-sessions-genex-mvp-2026

# Verify
gsutil ls -p genex-mvp-2026
```

The bucket stores two prefixes:
- `sessions/`  — de-identified interview snapshots (auto-saved after interview)
- `feedback/`  — advisor feedback form submissions

---

## Step 3 — Build the container image

Run from inside the `genex-alpha/` folder.

```bash
cd /Users/sara/Projects/Genex/genex-alpha

gcloud builds submit \
  --tag us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --project genex-mvp-2026 \
  .
```

Cloud Build runs serverless — no local Docker installation required.

**Optional: test the image locally before pushing**

```bash
# Requires Docker Desktop running on your Mac
docker build -t genex-alpha-local .
docker run -p 8080:8080 -e PORT=8080 genex-alpha-local
# Open http://localhost:8080 — should load Genex welcome screen
# Ctrl+C to stop
```

---

## Step 4 — Deploy to Cloud Run

Note: OPENAI_API_KEY is intentionally omitted here. The app runs fully on deterministic
fallbacks without it. Add the key via Secret Manager after confirming the deploy works
(see "Adding the OpenAI API key" section at the bottom).

```bash
gcloud run deploy genex-alpha \
  --image us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --no-allow-unauthenticated \
  --set-env-vars "GCS_BUCKET=genex-alpha-sessions-genex-mvp-2026,SESSION_DIR=/tmp/sessions" \
  --project genex-mvp-2026
```

Cloud Run prints the service URL when done:
`https://genex-alpha-xxxxxxxxxx-uc.a.run.app`

**Do not share this URL yet.** With `--no-allow-unauthenticated`, visiting it without
an authenticated token returns HTTP 403. It is not accessible in a browser by default.

---

## Step 5 — Grant GCS write access to the Cloud Run service account

Cloud Run uses the project's default Compute Engine service account. It needs permission
to write files to the sessions bucket.

```bash
PROJECT_NUMBER=$(gcloud projects describe genex-mvp-2026 --format="value(projectNumber)")

gsutil iam ch \
  serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com:roles/storage.objectCreator \
  gs://genex-alpha-sessions-genex-mvp-2026
```

---

## Phase 1 — Sara's private test (gcloud proxy)

**Who this is for:** Sara only. This is not an advisor-friendly link. It requires the
gcloud CLI and runs only as long as the terminal is open. Advisors cannot use this method
— they would need gcloud installed, authenticated, and the terminal command running on
their own computer. Use Phase 2A or 2B to share with advisors.

```bash
# Grant your account Cloud Run invoker permission
gcloud run services add-iam-policy-binding genex-alpha \
  --region us-central1 \
  --member="user:sara@getgenex.com" \
  --role="roles/run.invoker" \
  --project genex-mvp-2026

# Start the secure local proxy (keep this terminal window open)
gcloud run services proxy genex-alpha \
  --region us-central1 \
  --project genex-mvp-2026 \
  --port 8080
```

Open **http://localhost:8080** in your browser. The proxy authenticates using your
Google identity automatically — no password prompt appears in the app.

**Phase 1 test checklist before moving to Phase 2:**
- [ ] Welcome screen loads
- [ ] Can complete full interview end-to-end
- [ ] Results page shows domain cards (no huge metric font)
- [ ] Weekly plan generates with activity detail
- [ ] Doctor prep notes render correctly
- [ ] Download button produces a `.txt` summary
- [ ] Feedback form submits without error
- [ ] Session JSON appears in GCS: `gsutil ls gs://genex-alpha-sessions-genex-mvp-2026/sessions/`
- [ ] Feedback JSON appears in GCS: `gsutil ls gs://genex-alpha-sessions-genex-mvp-2026/feedback/`

---

## Phase 2A — Advisor access via Google login (IAP + Load Balancer)

**Who this is for:** Non-technical advisors who have a Google account (Gmail or Google
Workspace). They visit a URL like `https://alpha.getgenex.com`, see a standard Google
login page, and land directly in the app. No gcloud, no passwords, no app install needed.

**What this requires:** A domain you control (e.g. `alpha.getgenex.com`), ~60 min setup,
and ~$18/month for the Load Balancer.

**What advisors experience:**  
→ Visit `https://alpha.getgenex.com`  
→ Google login page appears  
→ They log in with their Google account  
→ If their email is on the approved list, they land in the app  
→ If not, they see an access denied page  

```bash
DOMAIN="alpha.getgenex.com"
PROJECT="genex-mvp-2026"
REGION="us-central1"

# 1. Reserve a global static IP address
gcloud compute addresses create genex-alpha-ip \
  --network-tier=PREMIUM \
  --ip-version=IPV4 \
  --global \
  --project $PROJECT

# Get the reserved IP — you will need this for DNS
gcloud compute addresses describe genex-alpha-ip \
  --global \
  --project $PROJECT \
  --format="value(address)"
# Example output: 34.120.XX.XX

# 2. Add a DNS record at your domain registrar
#    Type:  A
#    Name:  alpha          (creates alpha.getgenex.com)
#    Value: <IP from above>
#    TTL:   300
#
# Wait for DNS to propagate before continuing. Check:
#   dig alpha.getgenex.com
# You should see the IP you just reserved.

# 3. Create a serverless Network Endpoint Group pointing to the Cloud Run service
gcloud compute network-endpoint-groups create genex-alpha-neg \
  --region=$REGION \
  --network-endpoint-type=serverless \
  --cloud-run-service=genex-alpha \
  --project $PROJECT

# 4. Create a backend service
gcloud compute backend-services create genex-alpha-backend \
  --load-balancing-scheme=EXTERNAL \
  --global \
  --project $PROJECT

# 5. Attach the NEG to the backend service
gcloud compute backend-services add-backend genex-alpha-backend \
  --global \
  --network-endpoint-group=genex-alpha-neg \
  --network-endpoint-group-region=$REGION \
  --project $PROJECT

# 6. Create a URL map
gcloud compute url-maps create genex-alpha-url-map \
  --default-service=genex-alpha-backend \
  --global \
  --project $PROJECT

# 7. Create a Google-managed SSL certificate for your domain
#    Google provisions this automatically — takes 10–30 min after DNS propagates
gcloud compute ssl-certificates create genex-alpha-cert \
  --domains=$DOMAIN \
  --global \
  --project $PROJECT

# 8. Create an HTTPS target proxy
gcloud compute target-https-proxies create genex-alpha-https-proxy \
  --ssl-certificates=genex-alpha-cert \
  --url-map=genex-alpha-url-map \
  --global \
  --project $PROJECT

# 9. Create the forwarding rule that ties the static IP to the proxy
gcloud compute forwarding-rules create genex-alpha-https-rule \
  --load-balancing-scheme=EXTERNAL \
  --network-tier=PREMIUM \
  --address=genex-alpha-ip \
  --target-https-proxy=genex-alpha-https-proxy \
  --ports=443 \
  --global \
  --project $PROJECT

# 10. Enable IAP on the backend service
#     You MUST do this step in the Cloud Console (UI) first:
#       console.cloud.google.com → Security → Identity-Aware Proxy
#       Find "genex-alpha-backend" → toggle the IAP switch ON
#       Accept the OAuth consent screen prompts if shown
#
#     Then grant access via CLI:
gcloud iap web add-iam-policy-binding \
  --resource-type=backend-services \
  --service=genex-alpha-backend \
  --member="user:sara@getgenex.com" \
  --role="roles/iap.httpsResourceAccessor" \
  --project $PROJECT

# To add each advisor (repeat for each email):
gcloud iap web add-iam-policy-binding \
  --resource-type=backend-services \
  --service=genex-alpha-backend \
  --member="user:ADVISOR_EMAIL@gmail.com" \
  --role="roles/iap.httpsResourceAccessor" \
  --project $PROJECT
```

After the SSL cert provisions (10–30 min), advisors can visit `https://alpha.getgenex.com`
and use the app with their Google account. No instructions needed on their end.

**To remove an advisor's access:**
```bash
gcloud iap web remove-iam-policy-binding \
  --resource-type=backend-services \
  --service=genex-alpha-backend \
  --member="user:ADVISOR_EMAIL@gmail.com" \
  --role="roles/iap.httpsResourceAccessor" \
  --project genex-mvp-2026
```

---

## Phase 2B — Temporary password gate (quick advisor access, no Google account required)

**Who this is for:** A small group of trusted advisors for a very short alpha window,
where simplicity matters more than strict security. The app becomes publicly reachable
(no Google login required) but protected by a shared password that advisors enter the
first time they visit.

**Important caveats:**
- The URL is technically public — anyone with the link who guesses the password gets in
- Not suitable for sensitive data. For Genex alpha (de-identified: nickname + age only)
  this is an acceptable trade-off for a short window
- Switch to Phase 2A as soon as advisors are settled
- The password is stored as a Cloud Run environment variable, not in code

**Step 1 — Add the password check to app.py**

Add this function near the top of `app.py` and call it as the very first line of `main()`:

```python
def _password_gate():
    """Simple shared-secret gate for Phase 2B advisor access.
    Remove this when switching to Phase 2A (IAP).
    Controlled by ADVISOR_PASSWORD env var — if not set, gate is disabled.
    """
    import os
    required = os.environ.get("ADVISOR_PASSWORD", "").strip()
    if not required:
        return  # No password set — gate is open (local dev or Phase 2A)

    if st.session_state.get("_authenticated"):
        return  # Already passed this session

    st.markdown("## 🔒 Genex Advisor Alpha")
    st.caption("Enter the access password to continue.")
    pwd = st.text_input("Password", type="password", key="_pwd_input")
    if st.button("Enter", type="primary"):
        if pwd == required:
            st.session_state["_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()
```

In `main()`, add as the first line:
```python
def main():
    if "screen" not in st.session_state:
        st.session_state["screen"] = "welcome"
    _password_gate()   # ← add this line
    sidebar_nav()
    ...
```

**Step 2 — Redeploy with `--allow-unauthenticated` and the password env var**

```bash
gcloud run deploy genex-alpha \
  --image us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --allow-unauthenticated \
  --set-env-vars "GCS_BUCKET=genex-alpha-sessions-genex-mvp-2026,SESSION_DIR=/tmp/sessions,ADVISOR_PASSWORD=CHOOSE_A_STRONG_PASSWORD" \
  --project genex-mvp-2026
```

Replace `CHOOSE_A_STRONG_PASSWORD` with something like `genex-alpha-2026` or any
memorable phrase. Share it with advisors over a secure channel (Signal, not email).

The Cloud Run URL from Step 4 is now directly shareable — advisors visit it in any
browser, enter the password once per session, and are in.

**To shut off access immediately:** redeploy without `ADVISOR_PASSWORD` set, or
switch back to `--no-allow-unauthenticated`.

---

## Viewing saved data

```bash
# List session files
gsutil ls gs://genex-alpha-sessions-genex-mvp-2026/sessions/

# Download all sessions
gsutil -m cp -r gs://genex-alpha-sessions-genex-mvp-2026/sessions/ ./downloaded-sessions/

# List feedback files
gsutil ls gs://genex-alpha-sessions-genex-mvp-2026/feedback/

# Download all feedback
gsutil -m cp -r gs://genex-alpha-sessions-genex-mvp-2026/feedback/ ./downloaded-feedback/
```

---

## Redeployment (after code changes)

```bash
cd /Users/sara/Projects/Genex/genex-alpha

# 1. Build and push new image
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --project genex-mvp-2026 \
  .

# 2. Redeploy — Cloud Run does a zero-downtime rolling update automatically
gcloud run deploy genex-alpha \
  --image us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --platform managed \
  --region us-central1 \
  --project genex-mvp-2026
```

---

## Rollback

```bash
# List recent revisions
gcloud run revisions list \
  --service genex-alpha \
  --region us-central1 \
  --project genex-mvp-2026

# Send 100% of traffic back to a specific revision
gcloud run services update-traffic genex-alpha \
  --to-revisions=genex-alpha-XXXXXXXX=100 \
  --region us-central1 \
  --project genex-mvp-2026
```

Replace `genex-alpha-XXXXXXXX` with the revision name from the list above.

---

## Adding the OpenAI API key (after first successful deploy)

Do not put the API key in environment variables in plain text. Use Secret Manager.

```bash
# Store the key in Secret Manager
echo -n "sk-YOUR_KEY_HERE" | gcloud secrets create OPENAI_API_KEY \
  --data-file=- \
  --project genex-mvp-2026

# Grant Cloud Run's service account permission to read it
PROJECT_NUMBER=$(gcloud projects describe genex-mvp-2026 --format="value(projectNumber)")
gcloud secrets add-iam-policy-binding OPENAI_API_KEY \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project genex-mvp-2026

# Redeploy with the secret mounted as an env var
gcloud run deploy genex-alpha \
  --image us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --platform managed \
  --region us-central1 \
  --set-env-vars "GCS_BUCKET=genex-alpha-sessions-genex-mvp-2026,SESSION_DIR=/tmp/sessions" \
  --set-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest" \
  --project genex-mvp-2026
```

---

## Pre-deployment checklist

- [ ] `.env` is NOT in git — run `git status` and confirm it is absent
- [ ] `data/cdc_milestones.xlsx` is present in the repo (not excluded by .gitignore)
- [ ] Artifact Registry repository `genex-alpha` exists in `us-central1` (Step 1)
- [ ] GCS bucket `genex-alpha-sessions-genex-mvp-2026` exists (Step 2)
- [ ] Image built and pushed successfully (Step 3)
- [ ] Cloud Run service deployed with `--no-allow-unauthenticated` (Step 4)
- [ ] `GCS_BUCKET=genex-alpha-sessions-genex-mvp-2026` is in Cloud Run env vars
- [ ] Cloud Run service account has `roles/storage.objectCreator` on the bucket (Step 5)
- [ ] Sara can access via `gcloud run services proxy` and app loads (Phase 1)
- [ ] Full end-to-end test completed (all Phase 1 test checklist items)
- [ ] Session JSON appears in `gs://genex-alpha-sessions-genex-mvp-2026/sessions/`
- [ ] Feedback JSON appears in `gs://genex-alpha-sessions-genex-mvp-2026/feedback/`
- [ ] OPENAI_API_KEY added via Secret Manager after first deploy confirmed working

---

## Cost estimate

| Resource | Phase 1 | Phase 2A (IAP+LB) | Phase 2B (password) |
|---|---|---|---|
| Cloud Run (~10 sessions/day) | ~$0–2/mo | ~$0–2/mo | ~$0–2/mo |
| Artifact Registry | ~$0 | ~$0 | ~$0 |
| GCS bucket (<1 MB/mo) | ~$0 | ~$0 | ~$0 |
| Global Load Balancer | — | ~$18/mo | — |
| **Total** | **~$0–2/mo** | **~$18–20/mo** | **~$0–2/mo** |
