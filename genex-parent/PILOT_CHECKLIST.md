# Genex Parent Copilot — 5-Family Pilot Checklist

Version: v0.2 | Date: May 2026

---

## 1. What to test before sending the link

Run through the full flow yourself at least once with a realistic test case.

**Profile screen**
- [ ] Enter a child name, age, diagnosis, and concern — confirm all fields save correctly
- [ ] Try a very young age (e.g. 6 months) and an older age (e.g. 48 months) — confirm the interview adapts
- [ ] Try leaving the concern field vague (e.g. "general delay") — confirm it still generates a plan

**Interview**
- [ ] Answer some questions "Not yet" to trigger the 2-fail stopping rule — confirm it stops the domain early
- [ ] Use the Back button — confirm it returns to the previous question correctly
- [ ] Answer all questions — confirm the "Building plan..." screen appears and resolves

**Weekly Planner**
- [ ] Confirm the "plan is ready" banner appears exactly once
- [ ] Open an activity card — confirm it shows Why it helps, How to do it, and What you need (no "What to watch for")
- [ ] Remove an activity — confirm it disappears and does not return on rerun
- [ ] Add an activity from the bank — confirm it appears in the correct day
- [ ] Check that Saturday and Sunday show weekend-appropriate activities

**Doctor's Note**
- [ ] Confirm the note generates without clinical tier labels or scores
- [ ] Confirm the text is safe to copy into a medical appointment note

**Feedback**
- [ ] Submit feedback — confirm success message appears
- [ ] Check GCS bucket for the feedback JSON file (see section 5 below)

**Password gate**
- [ ] Confirm the app asks for a password on first load
- [ ] Confirm wrong password is rejected
- [ ] Confirm correct password lets you through

---

## 2. What parents should NOT enter

Brief parents on privacy before sharing the link.

- **No full name** — first name only (or a nickname / initial)
- **No date of birth** — enter age in months only (e.g. "18 months")
- **No school name, therapist name, or doctor name**
- **No insurance or medical record numbers**
- **No photos, videos, or documents** (the app does not accept these, but remind parents anyway)
- **No other family members' names or information**

Note for your records: The app stores child first name + age in months + diagnosis/concern in the
GCS session JSON. These are low-sensitivity fields but should still be handled with care.

---

## 3. Feedback questions to ask parents

After each family completes the app, ask these questions (by message, call, or form):

**Usefulness**
1. Did the weekly plan feel relevant to your child's actual needs?
2. Were the activities realistic to do at home?
3. Did the Doctor's Note feel like something you could actually use at an appointment?

**Clarity**
4. Were any instructions confusing or hard to follow?
5. Were there any words or terms you didn't understand?
6. Did the interview questions make sense for your child?

**Trust**
7. Did the app feel safe and respectful to use?
8. Was there anything that made you uncomfortable?

**Completeness**
9. Was there anything important about your child that the app didn't ask about?
10. What was missing that you wish the app had?

**Open**
11. What would make you want to use this again next week?

---

## 4. What counts as a serious bug

Stop the pilot and fix before continuing if any of the following occur:

- [ ] **App crashes** during the interview, plan generation, or Doctor's Note — white screen or Python traceback visible
- [ ] **Wrong plan generated** — activities clearly don't match the child's age or domain (e.g. walking activities for a 6-month-old)
- [ ] **Feedback not saved** — parent submits feedback but no file appears in GCS within 2 minutes
- [ ] **Password bypassed** — app loads without asking for a password
- [ ] **Clinical labels shown to parent** — tier names (e.g. "needs_special_support") appear anywhere in the UI
- [ ] **Interview loops infinitely** — questions keep repeating without advancing
- [ ] **Session data from one family visible to another** — any cross-session data leak

Non-blocking issues (log and fix in v0.3):
- Activity wording feels generic or off
- Minor layout issues on mobile
- Doctor's Note phrasing could be improved
- "Add activity" bank has limited options

---

## 5. What to check in GCS after each parent submits feedback

**List all files in the bucket:**
```bash
gcloud storage ls gs://genex-parent-sessions-genex-mvp-2026/
```

**List only feedback files:**
```bash
gcloud storage ls gs://genex-parent-sessions-genex-mvp-2026/feedback/
```

**List only session files:**
```bash
gcloud storage ls gs://genex-parent-sessions-genex-mvp-2026/sessions/
```

**Download and read a specific feedback file:**
```bash
gcloud storage cp gs://genex-parent-sessions-genex-mvp-2026/feedback/<filename>.json .
cat <filename>.json
```

**Download all feedback files at once:**
```bash
mkdir -p ~/pilot-feedback
gcloud storage cp "gs://genex-parent-sessions-genex-mvp-2026/feedback/*.json" ~/pilot-feedback/
```

**After downloading, run the feedback review script:**
```bash
python3 review_feedback.py ~/pilot-feedback/
```

This produces a CSV summary — see `review_feedback.py` for details.

---

## 6. After the pilot — deciding v0.3 scope

Collect answers to these questions from the pilot before scoping v0.3:

- Did parents return to the app more than once? (check session timestamps)
- Did parents use the Doctor's Note? Did it help?
- Were activities marked as "not relevant" verbally? (argument for completion tracking)
- Did parents ask for progress visibility across weeks?
- Were there domains or age ranges where the plan felt weak?

Planned v0.3 candidates (do not build until pilot is complete):
- Activity completion tracking ("Mark as done" per activity)
- Week-over-week progress view
- Activity rating / thumbs up-down per activity
- Better activity bank depth (more variety per domain)
