# CUSTOM INTEGRATION GUIDE - For Your Specific Setup
# ====================================================

## ✅ What I Found in Your Setup

Based on your screenshots and files, here's your current structure:

### Root Folders:
- ✅ **src/** (agents/, app/, dashboard/, mvp/) - YOUR REFERENCE CODE
- ✅ **notebooks/** - Already exists!
- ✅ **prototypes/** - Already exists!
- ✅ **webapp/** - From my Phase 1 files
- ✅ **data/** - Your data folder
- ✅ **.venv/** - Virtual environment (safe)
- ✅ **.git/** - Git repo (safe)
- Plus: agents/, app/, deck/, docs/, hpo/, images/, job/, logo/, orpha/, video/, work on it later/

### Root Files:
- ✅ **.env** - Has your 3 API keys
- ✅ **.gitignore** - Has good rules
- ✅ **.gitattributes** - Git LFS for large files
- ✅ **requirements.txt** - In hpo/ folder (data science packages)
- ✅ **milestone-cdc-table.xlsx** - CDC data
- ✅ **Genex_workflow.pdf** - Documentation
- ✅ **files.zip** - Archive

---

## 🎯 SAFE INTEGRATION STRATEGY

### PRINCIPLE: **PRESERVE & EXTEND**
- Keep src/ as your reference (untouched)
- notebooks/ and prototypes/ already exist (good!)
- Add Flask webapp/ structure alongside
- Merge configuration files safely

---

## 📋 STEP-BY-STEP INTEGRATION

### Step 1: Download All New Files

You already have these downloaded. Place them in your GENEX root:

```
GENEX/
├── custom_integration.py       ← NEW (safe script)
├── .env.integrated            ← NEW (your keys + Flask config)
├── .gitignore.merged          ← NEW (your rules + Flask rules)
├── requirements.merged.txt    ← NEW (your packages + Flask)
├── run.py                     ← NEW (Flask entry point)
├── README.md                  ← NEW (documentation)
├── SETUP_GUIDE.md            ← NEW (setup instructions)
└── webapp/                    ← NEW (extract from tar.gz)
```

### Step 2: Extract webapp/ Folder

```bash
# Extract the Flask application
tar -xzf genex_webapp_phase1.tar.gz

# You should now have a webapp/ folder
```

### Step 3: Run Custom Integration (Dry Run First)

```bash
# See what will happen (NO CHANGES)
python custom_integration.py

# Review the output carefully
```

This script will:
- ✅ Keep src/ completely untouched
- ✅ Keep notebooks/ and prototypes/ as-is
- ✅ Copy Genex_workflow.pdf to docs/
- ✅ Copy milestone-cdc-table.xlsx to data/ (if needed)
- ✅ Create FOLDER_REFERENCE_MAP.md

### Step 4: Execute Integration (If Happy)

```bash
python custom_integration.py --execute
```

### Step 5: Apply Configuration Files

```bash
# Backup current files first!
cp .env .env.backup
cp .gitignore .gitignore.backup

# Apply integrated versions
cp .env.integrated .env
cp .gitignore.merged .gitignore
cp requirements.merged.txt requirements.txt
```

### Step 6: Install Dependencies

```bash
# Make sure virtual environment is activated
.venv\Scripts\activate  # Windows
# or
source .venv/bin/activate  # Mac/Linux

# Install all packages (yours + Flask)
pip install -r requirements.txt
```

### Step 7: Generate SECRET_KEY

```bash
# Generate a secure secret key
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"

# Copy the output and add to your .env file
# Replace: SECRET_KEY=change-this-to-a-random-secret-key-in-production
```

### Step 8: Initialize Database

```bash
# Create Flask database tables
flask init-db

# Or manually:
python -c "from webapp import create_app, db; app = create_app(); app.app_context().push(); db.create_all()"
```

### Step 9: Run Flask Application

```bash
python run.py
```

Visit: **http://localhost:5000**

---

## 📁 YOUR FINAL STRUCTURE

After integration, you'll have:

```
genex/
├── .git/                          ← SAFE (Git repo)
├── .venv/                         ← SAFE (virtual env)
│
├── .env                           ← INTEGRATED (your keys + Flask config)
├── .gitignore                     ← MERGED (your rules + Flask rules)
├── .gitattributes                 ← KEPT (Git LFS)
├── requirements.txt               ← MERGED (data science + Flask)
├── run.py                         ← NEW (Flask entry point)
├── README.md                      ← NEW (docs)
├── FOLDER_REFERENCE_MAP.md        ← NEW (structure map)
│
├── src/                           ← PRESERVED (your reference code)
│   ├── agents/                   ← Your original agent notebooks
│   ├── app/                      ← Your original HTML prototypes
│   ├── dashboard/                ← Dashboard prototype code
│   └── mvp/                      ← MVP files
│
├── webapp/                        ← NEW (Flask application)
│   ├── __init__.py               ← App factory
│   ├── config.py                 ← Configuration
│   ├── models/                   ← Database models
│   ├── routes/                   ← URL routes
│   ├── agents/                   ← Production agents (Phase 2)
│   ├── services/                 ← Business logic (Phase 2)
│   ├── templates/                ← HTML templates (Phase 3)
│   ├── static/                   ← CSS/JS/images (Phase 3)
│   └── utils/                    ← Helper functions
│
├── notebooks/                     ← KEPT (your notebooks)
├── prototypes/                    ← KEPT (your prototypes)
├── data/                          ← KEPT (your data)
│   ├── milestone-cdc-table.xlsx  ← CDC data
│   └── genex_dev.db              ← NEW (Flask database)
│
├── docs/                          ← ORGANIZED (documentation)
│   └── Genex_workflow.pdf        ← Copied here
│
└── (all your other folders kept as-is)
    ├── agents/                    ← Your old notebooks folder
    ├── app/                       ← Your old HTML folder
    ├── deck/
    ├── hpo/                       ← Has your data science requirements.txt
    ├── images/
    ├── job/
    ├── logo/
    ├── orpha/
    ├── video/
    └── work on it later/
```

---

## 🔑 KEY POINTS

### What Gets PRESERVED Exactly As-Is:
1. ✅ **src/** folder - Complete reference of your original code
2. ✅ **.git/** - All your Git history
3. ✅ **.venv/** - Your Python environment
4. ✅ **notebooks/** - Already organized
5. ✅ **prototypes/** - Already organized
6. ✅ **All other folders** - deck/, hpo/, images/, etc.

### What Gets ADDED:
1. ✅ **webapp/** - New Flask application structure
2. ✅ **run.py** - Flask entry point
3. ✅ **Updated config files** - .env, .gitignore, requirements.txt
4. ✅ **Documentation** - README.md, guides

### What Gets COPIED (Not Moved):
1. ✅ **Genex_workflow.pdf** → docs/ (original stays in root)
2. ✅ **milestone-cdc-table.xlsx** → data/ (if not there already)

### Nothing Gets DELETED or MOVED:
- src/ stays exactly where it is
- All your other folders stay put
- We only ADD and COPY, never DELETE

---

## ✅ VERIFICATION CHECKLIST

After integration:

- [ ] src/ folder still exists with all content
- [ ] webapp/ folder exists with Flask code
- [ ] .env has your API keys + Flask config
- [ ] .gitignore has both your rules and Flask rules
- [ ] requirements.txt has data science + Flask packages
- [ ] Flask app starts: `python run.py`
- [ ] Can access http://localhost:5000
- [ ] Can create user account
- [ ] Database works (check data/genex_dev.db exists)

---

## 🆘 TROUBLESHOOTING

### "Module not found" errors
```bash
# Activate virtual environment
.venv\Scripts\activate

# Reinstall everything
pip install -r requirements.txt --upgrade
```

### Database errors
```bash
# Recreate database
rm data/genex_dev.db
flask init-db
```

### Flask won't start
```bash
# Check you're in GENEX root
pwd  # Should show path ending in /genex

# Check .env exists and has all variables
cat .env

# Try running with debug
python run.py
```

### Import errors from webapp
```bash
# Make sure you're running from root, not inside webapp/
cd /path/to/genex
python run.py
```

---

## 🎯 WHAT YOU'LL HAVE AFTER INTEGRATION

1. **Your Original Work (src/)** - Completely preserved as reference
2. **Flask Application (webapp/)** - New production structure
3. **Both Can Coexist** - src/ for reference, webapp/ for production
4. **Clean Organization** - notebooks/, prototypes/, data/ all organized
5. **Working Flask App** - Ready to run and test
6. **Safe Git History** - Nothing lost, everything tracked

---

## 🔜 NEXT: PHASE 2 (After Testing Phase 1)

Once Flask is running and you can:
- Create user accounts
- Create child profiles
- Database is working

Then we'll:
1. Convert src/agents/ code to webapp/agents/ (production versions)
2. Implement services layer
3. Connect agents to Flask routes
4. Add background processing

---

## 📞 NEED HELP?

Common issues and solutions:
1. Read FOLDER_REFERENCE_MAP.md for structure overview
2. Check README.md for detailed documentation
3. Review this guide's troubleshooting section
4. Make sure all steps were followed in order

---

**Your setup is now perfectly positioned for safe integration! 🎉**

**The custom_integration.py script is designed specifically for YOUR structure and will keep everything safe.**
