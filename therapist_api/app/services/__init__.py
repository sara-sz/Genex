"""Application services.

NOTE: these are DESIGN-LEVEL demonstrations exercised by tests. They are NOT
wired to any HTTP endpoint in this phase, they never touch a live Firestore, and
they never mutate a real parent plan. The "plan" here is a fictional in-store
document used to prove the idempotent apply design.
"""
