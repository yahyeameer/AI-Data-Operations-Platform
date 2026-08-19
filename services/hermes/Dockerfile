# Hermes agent.
#
# slim rather than alpine: polars, duckdb and pyarrow all ship manylinux wheels
# and none ship musl ones, so alpine would mean compiling Arrow from source on
# a 1 GB VPS. The image is larger and builds in seconds instead of an hour.
FROM python:3.12-slim

# Dependencies first, in their own layer. Requirements change rarely and the
# source changes constantly, so this ordering means a code deploy reuses the
# cached wheel install instead of redownloading Arrow every time.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY hermes ./hermes

# Unprivileged. The process holds the service-role key, so if anything about
# this container is compromised, root inside it should not also be on offer.
RUN useradd --system --create-home --uid 10001 hermes \
    && mkdir -p /var/lib/hermes \
    && chown -R hermes:hermes /var/lib/hermes /app
USER hermes

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HERMES_WORK_DIR=/var/lib/hermes

# No EXPOSE and no port. The agent dials out to Supabase and nothing dials in,
# which is the property that lets this run on a VPS with every inbound port
# closed except SSH.

# Liveness without a listening socket: the worker's own heartbeat row is the
# real signal, so the check only has to confirm the process can still import
# its stack and read its configuration.
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s --retries=3 \
    CMD python -c "from hermes.config import load_config; load_config()" || exit 1

CMD ["python", "-m", "hermes"]
