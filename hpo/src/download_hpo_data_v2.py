"""
download_hpo_data.py

Description:
    Script to download Human Phenotype Ontology (HPO) data files into the local
    project directory (`data_raw/hpo`). The script fetches ontology definitions
    and annotation files from official HPO sources and mirrors them locally
    for downstream analysis.

Contents:
    - fetch(urls, out: Path):
        Attempts to download a file from a list of URLs, writing the result to
        the given output path. Uses fallback URLs if the first fails.
    - main():
        Orchestrates downloading of all required HPO resources:
            * hp.json                → HPO ontology in JSON format
            * phenotype.hpoa         → Condition-to-phenotype associations
            * genes_to_phenotype.txt → Gene-to-phenotype associations
            * phenotype_to_genes.txt → Phenotype-to-gene associations

Usage:
    Run this script directly to populate `data_raw/hpo` with the latest HPO files:

        $ python hpo/src/download_hpo_data.py

Notes:
    - A custom User-Agent header is used to avoid request blocking.
    - SSL context is configured for secure HTTPS requests.
    - Files are written in binary mode to preserve formatting.

Author: Sara soltanizadeh
Created: 2025-09-08
"""

from __future__ import annotations
import ssl
import urllib.request
from pathlib import Path

# Set project root and destination folder for raw HPO data files.
# Because this file lives in hpo/src/, parents[1] points to hpo/
ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "data_raw" / "hpo"
DEST.mkdir(parents=True, exist_ok=True)

# Define User-Agent and SSL context to make secure, browser-like HTTP requests.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
CTX = ssl.create_default_context()

def fetch(urls, out: Path):
    """
    Try downloading a file from a list of URLs and save it to the destination.
    Returns True on success, False otherwise.
    """
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, context=CTX, timeout=120) as r, open(out, "wb") as f:
                f.write(r.read())
            print(f"{out.name} <- {u}")
            return True
        except Exception as e:
            print(f"fallback after {u}: {e}")

    print(f"✗ Could not fetch {out.name}. Download manually to {out}")
    return False

def main():
    """
    Download the main HPO ontology JSON plus annotation / gene mapping files.
    """
    fetch(
        ["https://purl.obolibrary.org/obo/hp.json"],
        DEST / "hp.json",
    )

    fetch(
        ["http://purl.obolibrary.org/obo/hp/hpoa/phenotype.hpoa"],
        DEST / "phenotype.hpoa",
    )

    fetch(
        ["http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt"],
        DEST / "genes_to_phenotype.txt",
    )

    fetch(
        ["http://purl.obolibrary.org/obo/hp/hpoa/phenotype_to_genes.txt"],
        DEST / "phenotype_to_genes.txt",
    )

if __name__ == "__main__":
    main()