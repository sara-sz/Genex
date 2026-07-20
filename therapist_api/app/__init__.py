"""Genex Therapist API (SLP-first) — isolated provider service.

This package is deliberately independent of the parent Genex backend:
  * It imports NOTHING from `genex_core`.
  * It imports NOTHING from the parent `api/` package.
  * It never touches parent GCS session objects.

All cross-product data access (e.g. reading a parent's plan snapshot) is a
FUTURE Parent Beta 2.4 concern and must go through an explicit, authenticated
parent-service contract — never direct storage access from here.
"""

__version__ = "0.1.0-alpha"
