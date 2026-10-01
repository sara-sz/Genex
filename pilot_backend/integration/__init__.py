"""pilot_backend/integration — boundaries to real Parent and Therapist systems.

Every adapter here is READ-ONLY against the source system. The pilot store is
the system of record for canonical identity and linkage; Parent remains the
system of record for Parent session data. 0.5A copies identity linkage only
and no clinical evidence.
"""
