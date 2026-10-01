"""pilot_backend/coding — versioned, deterministic CPT coding assistance.

ISOLATED from clinical and product domain logic on purpose (section 15). The
rule set can be replaced for a later year without touching the domain, and a
historical summary stays reproducible because it stores the rule-set id and
version it was generated under.

Nothing in this package imports a service, a repository or a store, and a
test asserts that. The rules are a pure function of documented facts.
"""
