# Genex UX Plan — Lovable Web App
Version: Pass 11 / June 2026  
Status: UX planning only — brain/scheduler frozen, not touched here.

---

## Step 1 — UX Audit of Current Streamlit App

### Current screens
1. Login / Register / Reset password
2. Privacy policy (full text, separate page)
3. Welcome (brief intro, start button)
4. Child profile (name, age in months, diagnosis, concern, daily time)
5. Interview (adaptive milestone questions, band by band, per domain)
6. Plan generation loading screen
7. Weekly plan dashboard (Mon–Fri cards, activity expanders, remove/add)
8. Doctor prep note
9. Feedback form
10. Sidebar navigation (always visible)

### What works well
- The adaptive question stopping rule (2 fails = stop) keeps interviews short
- The weekly plan gives concrete, specific activities — not generic tips
- Safety constraints work silently — parents never see "blocked for safety"
- Multi-concern routing covers both domains without parent noticing the complexity
- The doctor note is genuinely usable and non-clinical
- The "remove activity" feature gives parents real agency

### What feels too technical or Streamlit-like
- Progress bar and sidebar navigation feel like a dashboard tool, not a parenting app
- Milestone questions are displayed as raw Q&A without visual warmth
- "Domain" and "category" language occasionally leaks into UI labels
- The weekly plan is a long scrollable expander list — no visual hierarchy
- Activity cards inside expanders feel buried and hard to scan
- Font sizes and spacing are constrained by Streamlit defaults
- Register/login flow is plain — no warmth, no trust-building copy
- Privacy policy is full legal text with no plain-language summary
- "Daily time: X min" feels like a setting, not a personal commitment
- No illustrations, icons, or color differentiation between activity types
- No sense of progress or celebration after completing the flow

### What must be preserved exactly
- The full adaptive interview logic (do not rebuild this in Lovable)
- Safety constraint behavior (no jumping, no unsafe movement for high-fall profiles)
- Multi-concern routing (2 domains max, correct domains selected)
- The activity card content: title, instructions, materials, success, make_easier, make_harder, avoid, why
- The doctor note content and structure
- The allowlist-gated registration (only approved emails)
- GCS session storage (do not move to Supabase yet)
- AUTH_MODE=identity_platform (do not rebuild auth)
- The final plan gate (duplicate/unsafe/generic checks)

### What should be redesigned for Lovable
- All visual design: cards, typography, color, spacing, illustrations
- The onboarding flow: welcome → profile → concern → should feel like a conversation, not a form
- Milestone questions: one question at a time, full screen, large answer buttons
- Weekly plan: horizontal day selector + vertical activity cards, not expanders
- Activity detail: full-screen modal or card flip, not collapsed expander
- Feedback: 3-tap simple reaction, not a text form
- Progress: warm summary card, not a doctor note button buried at the bottom
- Loading screen: animated illustration, not a Streamlit spinner

### What should not be built yet
- Chatbot or conversational AI interface
- Community features / parent groups
- Nutrition or medication tracker
- Photo / video album
- Week-over-week progress graph (plan first, then data)
- Push notifications (plan first)
- Native mobile app (web-first)
- Supabase or new database (use existing GCS + Identity Platform)

---

## Step 2 — Page Map

```
App
├── /                          Welcome / landing inside app
├── /login                     Sign in
├── /register                  Create account (allowlist-gated)
├── /reset-password            Password reset
├── /privacy                   Privacy + consent (plain language summary + full text)
│
├── /onboarding
│   ├── /onboarding/profile    Child profile (name, age, diagnosis)
│   ├── /onboarding/concern    Parent concern + daily time commitment
│   └── /onboarding/questions  Adaptive milestone questions (one at a time)
│
├── /plan/loading              Plan generation — animated loading screen
│
├── /plan                      Weekly plan dashboard (main parent home)
│   ├── Day selector (Mon–Fri tabs)
│   ├── Activity cards per day
│   └── Add activity sheet
│
├── /plan/activity/:id         Activity detail (modal or full page)
│
├── /feedback/:activityId      After-activity feedback (3-tap reaction)
│
├── /progress                  Progress summary — what your child is practicing
│
├── /doctor-note               Doctor visit prep note (copy/download)
│
└── /settings
    ├── Privacy settings
    ├── Delete my data
    └── Change daily time / profile
```

---

## Step 3 — User Flow (Mobile-First, First Login to First Feedback)

### First-time parent (new account)

```
Open app
  → Welcome screen
      Headline: "A personal plan for your child, built around your day."
      CTA: "Get started" (warm purple button)

  → Sign up / sign in
      Email + password
      Allowlist check (silent — if blocked, warm "we'll be in touch" message)
      Consent checkbox: "I understand this is not a medical tool"

  → Child profile (step 1/3)
      "Tell us about your child"
      First name or nickname (optional — for personalisation only)
      Age in months (slider or number + descriptor: "24 months — 2 years")
      Diagnosis (optional dropdown: Down syndrome / ADHD / Dravet / other / none)
      
  → Parent concern (step 2/3)
      "What are you most focused on right now?"
      Free text with suggestion chips:
        [talking and words] [moving and balance] [attention and focus]
        [social connection] [daily routines] [something else]
      Daily time: "How many minutes can you spend on activities each day?"
        [5 min] [10 min] [15 min] [20+ min]

  → Questions (step 3/3)
      One question at a time, full screen
      Large friendly text: "Does [child name] wave goodbye?"
      Three answer buttons (large tap targets):
        [Yes, easily] [Sometimes / just starting] [Not yet]
      Progress dots at top (not a bar — less clinical)
      Skip option: "Not sure — skip this one"
      Stopping rule works silently — parents never see "we stopped early"

  → Loading screen
      Friendly illustration (plant growing / blocks stacking)
      Text: "Building your personalised plan…"
      Sub-text: "This takes just a moment."

  → Weekly plan dashboard
      "Here's your plan for this week"
      Day tabs: Mon / Tue / Wed / Thu / Fri
      Today's tab is active by default
      2 activity cards per day (at 10–15 min)
      Each card: icon + title + "5–15 min" + domain chip

  → Tap an activity card
      Full-screen modal or slide-up sheet
      Sections: How to do it / What you need / Why it helps / Make it easier / Make it harder
      Sticky bottom bar: [Done today ✓] [Too hard] [Skip]

  → After tapping "Done today"
      Quick reaction: "How did it go?"
      Three large emoji buttons: 😊 Went well / 😐 It was okay / 😔 Didn't work today
      Optional: "Add a note" (text field, optional)
      Confirmation: "Logged! Well done for trying."

  → Return to plan
      Activity card shows a soft ✓ checkmark
      Encouragement strip: "You've done 1 activity today."
```

### Returning parent (existing session)

```
Open app → straight to /plan (weekly plan dashboard, today's tab)
No re-onboarding unless they tap Settings → Update profile
```

---

## Step 4 — Component List for Lovable

### Navigation
- `TopBar` — app logo, settings icon, no sidebar
- `ProgressStepper` — dot-style progress for onboarding (3 steps), not a bar
- `DayTabStrip` — Mon/Tue/Wed/Thu/Fri horizontal scrollable tabs, today highlighted

### Onboarding
- `ProfileCard` — name field, age slider/input, diagnosis selector (optional)
- `ConcernInputCard` — free text + suggestion chips for concern; time selector chips
- `QuestionCard` — full-screen single question, child name personalised, large text
- `AnswerButton` — large tap target, 3 options (Yes / Sometimes / Not yet), rounded pill style
- `SkipLink` — small "Not sure, skip" text below answer buttons
- `LoadingScreen` — centered illustration + progress message, no spinner

### Weekly Plan
- `DayCard` — container for one day's activities
- `ActivityCard` — title, domain icon, duration label, brief teaser line, tap to expand
- `DomainChip` — small color-coded label (Language teal / Movement purple / Cognitive warm orange / Social warm yellow)
- `AddActivitySheet` — bottom sheet with a short list of bank activities to add

### Activity Detail
- `ActivityModal` — slide-up full sheet or full-page view
  - `ActivityHeader` — title + domain chip + duration
  - `InstructionBlock` — numbered steps, plain language
  - `MaterialsList` — simple icon list
  - `WhyBlock` — "Why it helps" collapsible (not shown by default to reduce overwhelm)
  - `AdaptBlock` — "Make it easier" and "Make it more playful" (replaces make_harder label)
  - `SafetyNote` — only shown when item has avoid text; soft yellow callout, not alarming
  - `ActivityActionBar` — [Done today] [Too hard] [Skip] — sticky bottom

### Feedback
- `FeedbackSheet` — slide-up after "Done today" tap
  - `ReactionRow` — 3 large emoji buttons
  - `FeedbackNote` — optional text field
  - `ConfirmMessage` — warm confirmation

### Progress & Doctor Note
- `ProgressSummaryCard` — "This week you tried X activities. [Child] is practicing: [domain list]"
- `DoctorNoteCard` — formatted note, copy button, download button
- `ActivityLogRow` — compact list of logged activities with date + reaction emoji

### Trust & Privacy
- `PrivacyFooter` — "Not a medical tool. Activities are for home use only." always visible at bottom of plan
- `ConsentCheckbox` — registration only, warm plain language
- `DeleteDataButton` — in settings, with confirmation dialog

---

## Step 5 — Copy / Tone Guide

### Genex voice
Warm, calm, practical, and non-judgmental. Speaks to the parent as a capable adult who knows their child best. Never clinical. Never alarming. Never preachy.

### Principles
1. **Parent as expert** — the app supports the parent, it does not instruct them
2. **Small wins matter** — celebrate any attempt, not just perfect performance
3. **Honest without being scary** — be clear about what the app is and isn't
4. **Specific, not vague** — "Roll the ball back and forth 3 times" not "engage in play"
5. **Warm, not cute** — no baby talk, no over-exclamation. Calm confidence.

### Words to use
| Instead of... | Use... |
|---|---|
| delay / deficit | "still developing" / "working on" |
| failed / not passed | "not yet" / "just starting" |
| treatment / therapy | "practice" / "activity" |
| score / assessment | "where your child is right now" |
| domain | "area" (only if label is needed) |
| clinical tier | never show this |
| you should | "you could try" |
| your child can't | "your child is working toward" |
| diagnosis | only use if parent entered it themselves |
| milestone | "step" or "skill" |

### Tone by screen
- **Welcome**: excited but calm. "Built for parents, not clinicians."
- **Onboarding**: conversational. "Tell us about your child."
- **Questions**: warm and curious. "Does [name] do this yet?"
- **Loading**: reassuring. "Building your personalised plan…"
- **Weekly plan**: actionable. "Here's what to try this week."
- **Activity detail**: instructional but friendly. Numbered steps, plain language.
- **Feedback**: celebratory and brief. "Logged! Well done for trying."
- **Doctor note**: professional but readable. No jargon, safe to share.
- **Privacy footer**: honest and brief. "Not a diagnostic tool."

### Micro-copy examples
- Empty state: "No activities logged yet this week — try one today."
- Loading: "Building your personalised plan… This takes just a moment."
- Feedback confirmation: "Logged! Well done for trying."
- After all activities done: "You've completed today's activities. That's a great day."
- Doctor note prompt: "Preparing for a therapy or doctor visit? Here's a summary you can share."
- Safety note (when present): "A gentle note: [avoid text]" — soft yellow, not red.
- Delete data confirmation: "Your data and your child's data will be permanently deleted. This cannot be undone."

---

## Step 6 — Lovable Prompts (Pass 1 — based on dear-journey preview, June 2026)

These prompts are safe to paste directly into Lovable. They are copy, layout, and visual-only changes. No backend, no auth, no brain, no storage changes.

### Adjustments locked in before implementation (June 2026)

**Diagnosis dropdown** — label: "Known diagnosis or condition, if any". Options: Down syndrome / ADHD / Dravet syndrome / Autism spectrum / Other / No known diagnosis / not sure / Prefer not to say. Do NOT include "Speech delay" as a diagnosis option — speech delay stays in the parent concern field only.

**Group/playdate section** — do not remove. Rename to "Try with a sibling or friend." Make it collapsed/optional, only show when content exists, keep it visually secondary below the main instructions.

**Prompt batching** — 3 batches. Review screenshots after each batch before proceeding.
- Batch 1: Branding + welcome (logo, copy, chips, warmth)
- Batch 2: Onboarding + questions (child profile, concern, diagnosis dropdown, question screen, loading screen)
- Batch 3: Weekly plan + activity detail (plan dashboard, cards, detail, feedback, reports, settings)

---

---

### Prompt 1 — Add Genex logo to welcome screen and all headers

```
On the welcome/splash screen (the first screen with "Get Started" and "I already have an account"), add the Genex logo image from public/assets/genex-logo.png. Place it centered, between the pagination dots at the top and the headline text. Give it a max-width of 200px and some bottom margin so it breathes.

On every logged-in screen that has a purple gradient header banner (Home, Care, Reports, Settings), add a small version of the Genex logo (public/assets/genex-logo.png) in the top-left corner of the header banner, max-height 32px. Make it white-tinted or full-color depending on contrast.

Do not change any backend logic, routing, or data.
```

---

### Prompt 2 — Fix welcome screen copy and feature chips

```
On the welcome screen, make the following copy changes only. Do not change layout, colors, or button behavior.

1. Change the headline from "Tailored to your child's unique needs" to "A weekly plan built around your child, starting where they are."

2. Change the subtitle from "AI-powered developmental support built around your child's genetics and milestones" to "Practical home activities, personalised to your child's age and what they're working on right now."

3. Replace the four feature chips with these exact labels:
   - "Personalised activities"
   - "5–15 minutes a day"
   - "Built around your child"
   - "Useful for doctor visits"
```

---

### Prompt 3 — Fix onboarding step 1 (child profile)

```
On the child profile step ("Tell us about your child / Let's start with the basics"):

1. Replace the "Date of Birth" date picker with an age input. Label it "How old is [child name]?" Accept a number. Show a subtitle below that updates dynamically: if the parent types 24, show "24 months — about 2 years old." Placeholder: "e.g. 18". Store as age in months.

2. Remove the Gender (optional) section entirely — the "Girl" and "Boy" emoji buttons and the label. Do not add a replacement field.

3. Keep the Child's Name field exactly as is.

Do not change routing, data storage, or any backend logic.
```

---

### Prompt 4 — Fix onboarding step 2 (diagnosis + concern)

```
On "Tell us about [child name]":

1. Change "Does [child name] have a known diagnosis?" from free text to a dropdown. Options:
   - Down syndrome
   - ADHD
   - Dravet syndrome
   - Autism spectrum
   - Speech delay
   - Other (show optional text field if selected)
   - No diagnosis
   - Prefer not to say

2. Remove the microphone/voice input button from the concern text field.

3. Change the concern field label to: "What are you most focused on for [child name] right now?" Update placeholder to: e.g. "She's working on walking independently and saying her first words."

Do not change routing, progress steps, or any backend logic.
```

---

### Prompt 5 — Fix transition screen and question screen

```
On the transition screen (puzzle piece icon, "We need to know [child name] more"):

1. Replace the puzzle piece icon with a star or sparkle icon.
2. Change headline to: "A few quick questions to personalise [child name]'s plan."
3. Change body text to: "We'll ask about what [child name] can do right now. There are no right answers — just what's true today."
4. Keep "Let's Go" button exactly as is.

On the question screen ("A few questions about [child name] / Question 1 of 5"):
1. Remove "Question 1 of 5" text. Show only progress dots — no count number.
2. Keep all other elements exactly as they are.
```

---

### Prompt 6 — Fix the loading screen

```
On the loading screen ("Creating [child name]'s Plan"):

1. Remove the 4-step checklist entirely (Analyzing developmental profile, Building personalized activities, Creating care schedule, Finalizing plan).

2. Keep the progress bar, simplified — single animated bar, no step labels.

3. Replace the checklist area with two centered lines:
   - Main (large, white, bold): "Building [child name]'s plan…"
   - Sub (smaller, white, 80% opacity): "This takes just a moment."

4. If the top animation is just a small white dot, replace with a pulsing white circle at 40px diameter.

Do not change timing, routing, or data logic.
```

---

### Prompt 7 — Clean up the Care Plan screen

```
On the Care Plan screen ("emma's Care Plan"):

1. Change the header subtitle from "Activities, nutrition & emergency plans" to "emma's weekly activity plan."
2. Remove the "Food & Med" and "Emergency" tabs from the tab strip. Show only the Activity tab.
3. Change "Request new evaluation" button to "Update emma's plan."
4. Change the header title from "emma's Care Plan" to "emma's Weekly Plan."

Do not change day tabs, activity cards, Accept/Personalize buttons, or routing.
```

---

### Prompt 8 — Clean up Home screen and Activity detail

```
On the Home screen:
1. Remove the streak element entirely (the "🔥 5 day streak!" line). Do not replace it.
2. Keep the progress ring, TODAY'S ACTIVITIES heading, and all activity cards.

On the Activity detail screen:
1. Remove the "Repetitions" stat block (the "Repetitions: 10 pick-ups" card). Keep Duration.
2. Remove the "Make it a group play / INCLUDE SIBLINGS OR FRIENDS" section entirely.
3. Keep Why we do this, How to do it, What you'll need, and the feedback section.

Do not change routing, feedback logic, or data.
```

---

### Prompt 9 — Fix feedback copy and remove Community from nav

```
In the "How did it go?" feedback section:
1. Change "Refused to do it" to "Didn't want to try."
2. Change "Couldn't do it" to "Wasn't ready yet."
3. Keep all other copy and behavior exactly as is.

In the bottom navigation bar:
1. Remove the "Community" tab and its icon. Bottom nav should show: Home, Care, Reports, More.

Do not change any other navigation items or routing.
```

---

### Prompt 10 — Fix Reports copy and Settings cleanup

```
On the Reports screen:
1. Change section heading "Activities by domain" to "What [child name] is working on."
2. Change the "For Provider" tab label to "Doctor note."
3. Keep all charts, stats, and domain bar labels as they are.

On the Settings screen:
1. Remove the "Activity Preferences / Customize activity types" row from the Preferences section.
2. Keep all other settings rows exactly as they are.

Do not change routing, toggle behavior, or data logic.
```

---

## Step 7 — Architecture Decision

### Recommended approach (safe, phased)

**Phase 1 — now (Lovable prototype with mocked data)**
- Lovable builds the full frontend UX with realistic mocked data
- No live backend connection
- No Supabase, no new database
- Goal: validate the UX, test with parents, get feedback on flow and feel
- Genex brain stays on staging Streamlit — unchanged

**Phase 2 — when UX is validated (add real data carefully)**
- Expose the Genex brain as a lightweight API (FastAPI wrapper around the existing genex_core pipeline)
- Lovable frontend calls the API: sends profile + answers, receives weekly plan JSON
- Auth stays on Google Identity Platform (existing)
- Storage stays on GCS (existing)
- OpenAI key stays in Secret Manager — never touches the frontend

**Phase 3 — later (if needed)**
- Move session data to Supabase only if you need cross-session queries, dashboards, or analytics that GCS JSON can't support
- Decide with explicit planning step — do not migrate without scoping

### Hard rules for Lovable integration
- Never put OpenAI API keys in the frontend or in Lovable env vars
- Never put GCS credentials in the frontend
- Do not rebuild the interview logic in Lovable — call the API
- Do not rebuild the safety/routing/gate logic in Lovable — call the API
- Do not connect Lovable to a live database until Phase 2 is explicitly scoped

---

## Step 8 — What to Build Now vs Later

### Build now (Lovable prototype, mocked data)
- Welcome / sign in / register screens (visual design)
- Child profile onboarding (3-step flow)
- Adaptive question screen (one question at a time, 3 answer buttons)
- Loading screen
- Weekly plan dashboard (day tabs + activity cards)
- Activity detail modal (all fields rendered)
- Feedback sheet (3-tap reaction)
- Privacy footer and consent screen

### Build next (once UX is validated with parents)
- Live connection to Genex API
- Real auth flow with Identity Platform
- Session save/load from GCS
- Progress summary and activity log
- Doctor note (pull from API)

### Do not build yet
- Push notifications
- Week-over-week graph
- Activity completion streaks
- Community / sharing
- Nutrition / medication
- Native mobile app
- Supabase migration
