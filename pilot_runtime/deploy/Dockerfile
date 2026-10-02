# FICTIONAL-DATA staging image for October pilot 0.5C.
#
# Not a production image. It is pinned, slim and credential-free, but it has
# not been through a production container review and the entrypoint it runs
# refuses PILOT_ENVIRONMENT=prod outright.
#
# Only `pilot_backend/` and `pilot_runtime/` are copied in. The repository
# root holds the Parent 2.3 service, the therapist service, notebooks and
# several GB of research assets, none of which the pilot imports — leaving
# them out keeps the image small and means a PHI-era review of this image has
# a small surface to read.
#
# This file lives under `pilot_runtime/` rather than at the repository root for
# an enforced reason, not a stylistic one: `PARENT_ALLOWED_PREFIXES` in
# genex-parent/tests/test_parent_24_integrity.py permits only `genex-parent/`,
# the Parent CI workflow, `pilot_backend/` and `pilot_runtime/`, and its
# docstring names deployment config as explicitly out of scope. A root-level
# `deploy/` fails `test_parent_commits_touch_only_parent_paths` — confirmed by
# committing it there and watching the guard reject both files. Same reason
# `requirements-integration-ci.txt` sits beside the code it installs.

FROM python:3.11.9-slim-bookworm

# No .pyc files, unbuffered logs so Cloud Run sees output immediately, and no
# pip version chatter in build logs.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the SDKs.
COPY pilot_runtime/deploy/requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY pilot_backend/ /app/pilot_backend/
COPY pilot_runtime/ /app/pilot_runtime/

# Tests are not part of a served image. Removing them also removes the
# fixtures and the fake document store from the runtime filesystem, so no
# in-memory persistence path is even present on disk.
RUN rm -rf /app/pilot_backend/tests /app/pilot_runtime/tests \
           /app/pilot_backend/fixtures \
    && find /app -name '__pycache__' -type d -prune -exec rm -rf {} +

# Unprivileged runtime user. Nothing in the service writes to the filesystem.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 pilot
USER 10001

EXPOSE 8080

# One worker, several threads: the service is I/O bound on Firestore and
# Identity Platform, and a single worker keeps one Firebase app per process.
#
# `--access-logfile -` is deliberately ABSENT. Cloud Run already records the
# request line at the platform layer; duplicating it inside the container
# would write path segments (which include child identifiers) into the
# application's own log stream for no diagnostic gain.
CMD exec gunicorn \
      --bind 0.0.0.0:${PORT:-8080} \
      --workers 1 \
      --threads 8 \
      --timeout 60 \
      --graceful-timeout 20 \
      --preload \
      --no-sendfile \
      pilot_runtime.server:application
