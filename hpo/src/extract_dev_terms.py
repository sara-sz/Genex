"""
GeneX: HPO Developmental / Therapy Term Extractor
-------------------------------------------------
This script searches the Human Phenotype Ontology (HPO) JSON file for
terms that are relevant to *developmental*, *speech/language*, *motor*,
*cognitive*, *learning*, or *social* domains — i.e., those typically linked
to therapeutic or developmental assessments.

Input:
    • data_raw/hpo/hp.json — Full Human Phenotype Ontology dataset (JSON format)
      containing all HPO terms with IDs, names, and definitions.

Process:
    1. Loads the HPO ontology using the `pronto` library.
    2. Iterates through all ontology terms.
    3. Filters for terms whose labels contain any of the target keywords:
       ["delay", "development", "speech", "language", "motor", "cognitive", "learning", "social"].
    4. Collects matching terms (HPO ID, label, definition, matched keyword).

Output:
    • data_proc/dev_therapy_terms.csv — A filtered CSV containing only
      HPO terms related to developmental or therapy-relevant concepts.

Columns:
    - feature_id : HPO term ID (e.g., HP:0001263)
    - label      : Original HPO term label
    - definition : Ontology definition text (if available)
    - keyword    : The matched keyword that triggered inclusion

Example:
    HP:0001263 | Global developmental delay | "Significant delay in multiple developmental domains." | keyword="development"

Intended Use:
    This file serves as the foundation for downstream filtering and
    therapy tagging (see `clean_dev_therapy_terms.py`), where terms are
    grouped by therapy relevance (Speech, OT, PT, etc.) and cleaned of
    anatomical or unrelated items.
"""

import pandas as pd
from pronto import Ontology

# === 1. Load ontology ===
HPO_PATH = "data_raw/hpo/hp.json"
print("Loading HPO ontology...")
hpo = Ontology(HPO_PATH)

# === 2. Define keywords for developmental / therapy terms ===
keywords = [
    "delay",
    "development",
    "speech",
    "language",
    "motor",
    "cognitive",
    "learning",
    "social",
]

# === 3. Search ontology ===
results = []
for kw in keywords:
    for term in hpo.terms():
        if kw.lower() in term.name.lower():
            results.append({
                "feature_id": term.id,
                "label": term.name,
                "definition": term.definition,
                "keyword": kw
            })

df = pd.DataFrame(results).drop_duplicates()

# === 4. Save to CSV ===
output_path = "data_proc/dev_therapy_terms.csv"
df.to_csv(output_path, index=False)

print(f"Saved {len(df)} developmental/therapy-related terms to {output_path}")
print(df.head(10))
