"""Deployment artifacts and one-off seed tooling for the pilot service.

A package rather than a loose directory so `python -m
pilot_runtime.deploy.seed_fictional` works inside the container, which is
where the seed has to run: the host has no Application Default Credentials,
and creating a service-account key to give it some would mean putting a
long-lived credential on a laptop to save a two-minute Cloud Run job.

Nothing here is imported by the serving path. `pilot_runtime/server.py` does
not import this package, so the seed is absent from the request path even
though it ships in the same image.
"""
