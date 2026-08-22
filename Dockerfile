# Ledgerline. One image, three processes -- see fly.toml.
#
# Two stages, and the split earns its keep here rather than being ceremony: asyncpg
# and greenlet ship wheels for this platform, but a wheel-less transitive dependency
# would otherwise drag a C toolchain into the runtime image forever. Building in a
# stage that is thrown away means the runtime never has a compiler in it, which is
# both smaller and one fewer thing for an attacker to find useful.

# --- build ------------------------------------------------------------------------
# 3.13, matching the local venv, ruff's target-version and the CI runner exactly.
# This used to say 3.12 -- so the container ran a Python the 168 tests had never
# been executed against, which is the sort of difference that stays invisible until
# it is the explanation for something.
FROM python:3.13-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copied alone, before the source, so a source-only change reuses this layer. The
# dependency install is the slow step and it changes far less often than the code.
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- runtime ----------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # Deployed images run as production unless something says otherwise, which is
    # what disables the fake processor's test knobs and makes the naive strategy
    # paths refuse to start. Defaulting here rather than only in fly.toml means an
    # image run anywhere else is hardened too.
    APP_ENV=production \
    LOG_FORMAT=json

# Not root. A container process that does not need to write to its own filesystem
# should not be able to, and the cost of this is two lines.
RUN useradd --create-home --uid 10001 ledgerline

WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY --chown=ledgerline:ledgerline alembic.ini ./
COPY --chown=ledgerline:ledgerline alembic ./alembic
COPY --chown=ledgerline:ledgerline app ./app

USER ledgerline

EXPOSE 8000

# The web process. fly.toml overrides this for the publisher and reconciler
# machines, which run the same image with a different command -- one image, three
# processes, so there is no way for the workers to be running different code than
# the API.
#
# No --reload and no --workers: Fly scales by running more machines, and a second
# uvicorn worker inside one machine would give this process two independent
# in-memory rate-limit windows (see app/ratelimit.py) for no benefit.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
