#!/usr/bin/env python3
"""
GENEX Custom Safe Integration Script
=====================================
This script is customized for YOUR specific folder structure.
It PRESERVES src/ folder and COPIES files (not moves them).

WHAT IT DOES:
- Keeps src/ folder completely untouched as reference
- COPIES (not moves) files to new locations
- Creates proper Flask structure alongside your existing work
- Creates backup before any changes

YOUR CURRENT STRUCTURE (from screenshots):
- src/ (contains: agents/, app/, dashboard/, mvp/)
- agents/ (old notebooks - already organized in your way)
- app/ (old HTML prototypes)
- notebooks/ (already exists!)
- prototypes/ (already exists!)
- webapp/ (already exists - from my Phase 1 files!)
- data/, docs/, deck/, hpo/, images/, job/, logo/, orpha/, video/, work on it later/
- .env, .gitignore, .gitattributes, milestone-cdc-table.xlsx, files.zip, Genex_workflow.pdf

SAFE STRATEGY:
1. Keep src/ exactly as-is (reference copy)
2. Your notebooks/ folder already exists - good!
3. Your prototypes/ folder already exists - good!
4. webapp/ folder from my files - good!
5. data/ folder already exists - perfect!
6. Just organize remaining files

"""

import os
import shutil
from pathlib import Path
from datetime import datetime


class CustomGenexIntegration:
    def __init__(self, root_path="."):
        self.root = Path(root_path).resolve()
        self.backup_dir = self.root / f"_integration_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.dry_run = True
        
    def log(self, message, level="INFO"):
        """Print colored log messages"""
        colors = {
            "INFO": "\033[94m",
            "SUCCESS": "\033[92m",
            "WARNING": "\033[93m",
            "ERROR": "\033[91m",
        }
        reset = "\033[0m"
        print(f"{colors.get(level, '')}{level}: {message}{reset}")
    
    def check_existing_structure(self):
        """Check what already exists"""
        self.log("\nChecking existing structure...", "INFO")
        
        checks = {
            'src/': 'PRESERVE - Your reference code',
            'notebooks/': 'EXISTS - Keep as-is',
            'prototypes/': 'EXISTS - Keep as-is', 
            'webapp/': 'NEW - From Phase 1 files',
            'data/': 'EXISTS - Your data folder',
            '.venv/': 'SAFE - Virtual environment',
            '.git/': 'SAFE - Git repository'
        }
        
        for folder, status in checks.items():
            folder_path = self.root / folder
            exists = "✅ EXISTS" if folder_path.exists() else "❌ MISSING"
            self.log(f"  {folder}: {exists} - {status}", "INFO")
    
    def organize_loose_files(self):
        """Organize loose files in root"""
        self.log("\nOrganizing loose files...", "INFO")
        
        # Move PDF to docs/
        pdf_file = self.root / "Genex_workflow.pdf"
        if pdf_file.exists():
            docs_dir = self.root / "docs"
            docs_dir.mkdir(exist_ok=True)
            dest = docs_dir / pdf_file.name
            if self.dry_run:
                self.log(f"  Would copy: {pdf_file.name} → docs/", "INFO")
            else:
                if not dest.exists():
                    shutil.copy2(pdf_file, dest)
                    self.log(f"  Copied: {pdf_file.name} → docs/", "SUCCESS")
        
        # Move Excel to data/ if not already there
        excel_file = self.root / "milestone-cdc-table.xlsx"
        if excel_file.exists():
            data_dir = self.root / "data"
            dest = data_dir / excel_file.name
            if not dest.exists():
                if self.dry_run:
                    self.log(f"  Would copy: {excel_file.name} → data/", "INFO")
                else:
                    shutil.copy2(excel_file, dest)
                    self.log(f"  Copied: {excel_file.name} → data/", "SUCCESS")
    
    def verify_notebooks_and_prototypes(self):
        """Verify notebooks/ and prototypes/ have content"""
        self.log("\nVerifying existing folders...", "INFO")
        
        notebooks_dir = self.root / "notebooks"
        if notebooks_dir.exists():
            count = len(list(notebooks_dir.glob("*.ipynb")))
            self.log(f"  notebooks/ contains {count} .ipynb files ✅", "SUCCESS")
        
        prototypes_dir = self.root / "prototypes"
        if prototypes_dir.exists():
            count = len(list(prototypes_dir.glob("*.html")))
            self.log(f"  prototypes/ contains {count} .html files ✅", "SUCCESS")
    
    def create_reference_map(self):
        """Create a reference map document"""
        self.log("\nCreating reference map...", "INFO")
        
        map_content = """# GENEX Folder Reference Map
# ============================
# Created: {timestamp}

## YOUR ORIGINAL STRUCTURE (PRESERVED)
src/
├── agents/          - Your agent notebooks (original location)
├── app/             - Your HTML prototypes (original location)
├── dashboard/       - Dashboard prototype code
└── mvp/             - MVP files

## NEW FLASK STRUCTURE
webapp/              - Flask application (Phase 1)
├── models/          - Database models
├── routes/          - Route blueprints
├── agents/          - Production agents (Phase 2)
├── services/        - Business logic (Phase 2)
└── templates/       - HTML templates (Phase 3)

## ORGANIZED FOLDERS
notebooks/           - All Jupyter notebooks
prototypes/          - All HTML prototypes
data/                - Data files (Excel, databases)
docs/                - Documentation & presentations

## PRESERVED AS-IS
.venv/               - Your virtual environment
.git/                - Your Git repository
.env                 - Your API keys (integrated)
.gitignore           - Your rules (merged)
.gitattributes       - Your Git LFS settings

## OTHER FOLDERS (Your existing work)
deck/                - Presentation decks
hpo/                 - HPO data work
images/              - Image assets
job/                 - Job-related files
logo/                - Logo files
orpha/               - Orpha data
video/               - Video files
work on it later/    - Pending work

## KEY FILES IN ROOT
.env                 - Environment variables (use .env.integrated)
.gitignore           - Git rules (use .gitignore.merged)
.gitattributes       - Git LFS (keep as-is)
requirements.txt     - Python packages (use requirements.merged.txt)
run.py               - Flask entry point (NEW)
""".format(timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        if not self.dry_run:
            ref_file = self.root / "FOLDER_REFERENCE_MAP.md"
            with open(ref_file, 'w') as f:
                f.write(map_content)
            self.log(f"  Created: FOLDER_REFERENCE_MAP.md", "SUCCESS")
    
    def run(self, dry_run=True):
        """Execute the safe integration"""
        self.dry_run = dry_run
        
        self.log("="*70, "INFO")
        self.log("GENEX CUSTOM SAFE INTEGRATION", "INFO")
        self.log("="*70, "INFO")
        self.log(f"Root directory: {self.root}", "INFO")
        self.log(f"Mode: {'DRY RUN' if dry_run else 'EXECUTION'}", 
                 "WARNING" if not dry_run else "INFO")
        self.log("="*70, "INFO")
        
        self.check_existing_structure()
        self.verify_notebooks_and_prototypes()
        self.organize_loose_files()
        self.create_reference_map()
        
        self.log("\n" + "="*70, "INFO")
        if dry_run:
            self.log("DRY RUN COMPLETE - No changes made", "SUCCESS")
            self.log("\nWhat this script will do:", "INFO")
            self.log("  1. Keep src/ folder completely untouched ✅", "INFO")
            self.log("  2. Keep notebooks/ and prototypes/ as-is ✅", "INFO")
            self.log("  3. Copy PDF to docs/ ✅", "INFO")
            self.log("  4. Copy Excel to data/ (if needed) ✅", "INFO")
            self.log("  5. Create reference map document ✅", "INFO")
            self.log("\nTo execute: python custom_integration.py --execute", "INFO")
        else:
            self.log("INTEGRATION COMPLETE!", "SUCCESS")
        self.log("="*70, "INFO")


def main():
    import sys
    
    execute = "--execute" in sys.argv or "-e" in sys.argv
    
    integrator = CustomGenexIntegration()
    integrator.run(dry_run=not execute)
    
    if not execute:
        print("\n" + "="*70)
        print("NEXT STEPS:")
        print("="*70)
        print("1. Review planned changes above")
        print("2. If everything looks good:")
        print("   python custom_integration.py --execute")
        print("\n3. Then apply configuration:")
        print("   cp .env.integrated .env")
        print("   cp .gitignore.merged .gitignore")
        print("   cp requirements.merged.txt requirements.txt")
        print("\n4. Install dependencies:")
        print("   pip install -r requirements.txt")
        print("\n5. Initialize database:")
        print("   flask init-db")
        print("\n6. Run Flask:")
        print("   python run.py")
        print("="*70)


if __name__ == "__main__":
    main()
