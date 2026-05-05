# src/download_orpha_data.py
"""
Download Orphadata (Orphanet) files needed for the genetics ML project.
Saved to: data_raw/orpha/

Run:
  .\.venv\Scripts\python -m src.download_orpha_data
"""

from __future__ import annotations
import os, sys, ssl, time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# --------- Config ---------
DEST_DIR = Path("data_raw/orpha")

# Each item: local filename -> list of candidate URLs (first that works wins)
FILES = {
    # ORDO ontology (OWL). Primary is Orphadata; PURL is a fallback but may return a tiny redirect.
    "ordo.owl": [
        "http://www.orphadata.org/data/ORDO/ordo_orphanet.owl",
        "https://www.orphadata.org/data/ORDO/ordo_orphanet.owl",
        "https://purl.obolibrary.org/obo/ordo.owl",
    ],
    # Phenotypes ↔ disorders (English XML)
    "en_product4.xml": [
        "https://www.orphadata.com/data/xml/en_product4.xml",
    ],
    # Genes ↔ disorders (English XML)
    "en_product6.xml": [
        "https://www.orphadata.com/data/xml/en_product6.xml",
    ],
    # Epidemiology / prevalence (English XML) – filename is stable as of recent releases
    "en_product9_prev.xml": [
        "https://www.orphadata.com/data/xml/en_product9_prev.xml",
    ],
    # Clinical classifications / hierarchies (English XML) – grab a few useful sets
    "en_product3_156.xml": [  # Rare genetic diseases
        "https://www.orphadata.com/data/xml/en_product3_156.xml",
    ],
    "en_product3_150.xml": [  # Rare inborn errors of metabolism
        "https://www.orphadata.com/data/xml/en_product3_150.xml",
    ],
    "en_product3_181.xml": [  # Rare neurological diseases
        "https://www.orphadata.com/data/xml/en_product3_181.xml",
    ],
    "en_product3_189.xml": [  # Rare ophthalmic diseases
        "https://www.orphadata.com/data/xml/en_product3_189.xml",
    ],
    "en_product3_202.xml": [  # Rare neoplastic diseases
        "https://www.orphadata.com/data/xml/en_product3_202.xml",
    ],
}

# Minimum byte sizes we consider "plausible" (helps catch saved HTML pages)
MIN_BYTES_HINT = {
    "ordo.owl": 5_000_000,           # ORDO OWL is big (tens of MB)
    "en_product4.xml": 10_000_000,   # large phenotype export
    "en_product6.xml": 5_000_000,    # genes export
    "en_product9_prev.xml": 1_000_000,  # prevalence varies but >1 MB
    # classifications vary by specialty; set low thresholds
    "en_product3_156.xml": 500_000,
    "en_product3_150.xml": 200_000,
    "en_product3_181.xml": 200_000,
    "en_product3_189.xml": 200_000,
    "en_product3_202.xml": 200_000,
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# --------- Helpers ---------
def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def pretty_size(n: int) -> str:
    for unit in ("B","KB","MB","GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"

def download_one(name: str, urls: list[str], dest: Path) -> bool:
    """Try each URL until one succeeds. Returns True if file saved."""
    target = dest / name
    if target.exists() and target.stat().st_size >= MIN_BYTES_HINT.get(name, 1):
        print(f"✓ {name} already present ({pretty_size(target.stat().st_size)}), skipping.")
        return True

    ctx = ssl.create_default_context()
    for url in urls:
        print(f"→ {name}  from  {url}")
        req = Request(url, headers={"User-Agent": UA})
        try:
            with urlopen(req, context=ctx, timeout=120) as r, open(target, "wb") as out:
                # stream in chunks
                total = 0
                while True:
                    chunk = r.read(1024 * 64)
                    if not chunk: break
                    out.write(chunk)
                    total += len(chunk)
            size = target.stat().st_size
            # quick sanity: avoid tiny HTML/redirects
            if size < MIN_BYTES_HINT.get(name, 1):
                # check if HTML
                with open(target, "rb") as fh:
                    head = fh.read(2000).lower()
                if b"<html" in head or b"<!doctype html" in head:
                    print(f"  ! got HTML (likely a landing page). Will try next URL.")
                    target.unlink(missing_ok=True)
                    continue
                else:
                    print(f"  ? warning: {name} is smaller than expected ({pretty_size(size)}). Keeping it.")
            print(f"✓ saved {name}  ({pretty_size(size)})")
            return True
        except (HTTPError, URLError, ssl.SSLError) as e:
            print(f"  x failed: {e}")
            # try next URL
            continue
        except Exception as e:
            print(f"  x unexpected error: {e}")
            continue

    print(f"✗ could not fetch {name}. You can download it manually into {dest}")
    return False

# --------- Main ---------
def main(argv: list[str]) -> int:
    ensure_dir(DEST_DIR)

    # Allow selecting a subset via CLI, e.g.:
    #   python -m src.download_orpha_data en_product4.xml ordo.owl
    wanted = list(FILES.keys()) if len(argv) == 0 else [a for a in argv if a in FILES]
    if not wanted:
        print("Nothing to do. Valid names:\n  " + "\n  ".join(FILES.keys()))
        return 1

    print(f"Destination: {DEST_DIR.resolve()}")
    ok_all = True
    for name in wanted:
        ok = download_one(name, FILES[name], DEST_DIR)
        ok_all = ok_all and ok

    print("\nDone.")
    return 0 if ok_all else 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
