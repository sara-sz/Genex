"""Integration tests that run against a REAL Firestore emulator.

Nothing in this package uses a fake store. If the emulator is unavailable
these tests FAIL — they never skip. A skipped integration suite reports the
same green as a passing one while proving nothing, and this is the suite whose
entire purpose is to show the real adapter works.
"""
