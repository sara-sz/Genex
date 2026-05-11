# Genex Advisor Alpha

Genex is a private developmental home-support planning tool for advisors working with parents of children ages 0–5. It generates a personalised weekly activity plan based on a structured milestone interview.

**Version:** v0.1 — Advisor Alpha  
**Status:** Private alpha — 4 advisors, no PHI, child nickname + age in months only

---

## What it does

1. Advisor enters a child profile (nickname, age in months, diagnosis, concern, daily time available)
2. App runs an adaptive milestone interview across four developmental domains
3. Scores answers, assigns a support tier per domain, and selects target milestones
4. Generates a 7-day home activity plan (Mon–Fri short activities, Sat–Sun extended/playdate)
5. Advisor can edit the plan, download it, and submit feedback

Scoring, tier assignment, and schedule logic are fully deterministic. OpenAI (GPT-4o-mini) is used only to write parent-friendly activity wording when an API key is configured.

---

## Run locally

### Prerequisites
- Python 3.10+
- A copy of `data/cdc_milestones.xlsx` in the project folder

### First-time setup
```bash
cd genex-alpha
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run
```bash
source .venv/bin/activate
streamlit run app.py
```

Open `http://localhost:8501` in your browser.

### Optional: OpenAI key for AI-generated activities
Create a `.env` file in the project root (never commit this):
```
OPENAI_API_KEY=sk-...
```
Without this key the app uses deterministic fallback activities — fully functional but less polished.

---

## Run with Docker locally

### Build
```bash
docker build -t genex-alpha .
```

### Run
```bash
docker run -p 8080:8080 \
  -e OPENAI_API_KEY=sk-... \
  genex-alpha
```

Open `http://localhost:8080`.

---

## Deploy to Google Cloud Run

Full step-by-step instructions are in [DEPLOY.md](DEPLOY.md).

### Quick reference — deploy latest code

**1. Build and push image**
```bash
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --project genex-mvp-2026 .
```

**2. Deploy**
```bash
gcloud run deploy genex-alpha \
  --image us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest \
  --platform managed \
  --region us-central1 \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 2 \
  --set-env-vars "GCS_BUCKET=genex-alpha-sessions-genex-mvp-2026,SESSION_DIR=/tmp/sessions" \
  --set-secrets "OPENAI_API_KEY=OPENAI_API_KEY:latest,ADVISOR_PASSWORD=ADVISOR_PASSWORD:latest" \
  --project genex-mvp-2026
```

**3. Test via proxy (Phase 1 / private access)**
```bash
gcloud run services proxy genex-alpha \
  --region us-central1 \
  --project genex-mvp-2026 \
  --port 8080
```
Open `http://localhost:8080`.

### GCP resources
| Resource | Value |
|---|---|
| Project | genex-mvp-2026 |
| Service | genex-alpha |
| Region | us-central1 |
| Artifact Registry image | `us-central1-docker.pkg.dev/genex-mvp-2026/genex-alpha/genex-alpha:latest` |
| GCS bucket | `genex-alpha-sessions-genex-mvp-2026` |
| Secrets | `OPENAI_API_KEY`, `ADVISOR_PASSWORD` |

---

## Project structure

```
genex-alpha/
├── app.py                  # Streamlit app — all 8 screens
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .gitignore
├── DEPLOY.md               # Full Cloud Run deployment guide
├── CHANGELOG.md
├── data/
│   └── cdc_milestones.xlsx # CDC milestone bank (2–60 months, 4 domains)
└── genex_core/
    ├── config.py           # Domain config, answer scores, keyword maps
    ├── milestones.py       # Milestone loader and question builder helpers
    ├── interview_engine.py # State init, concern router, milestone questions
    ├── scoring.py          # Developmental age scoring
    ├── delay_engine.py     # Delay estimation
    ├── support_tiers.py    # Tier assignment, family guidance floor, milestone selection
    ├── safety.py           # Safety profile and activity constraints
    ├── activity_engine.py  # Activity bank generation (OpenAI + deterministic fallback)
    ├── scheduler.py        # Weekly slot allocation and 7-day schedule builder
    ├── summaries.py        # Text summary generation
    └── storage.py          # GCS + local JSON persistence
```

---

## Privacy and safety

- No PHI. Child nickname and age in months only.
- No date of birth, full name, address, school, doctor, photos, or documents.
- Sessions saved as JSON to GCS — no relational database.
- This tool does not replace professional assessment or diagnosis.
- For advisor alpha only — not for clinical use or public distribution.
