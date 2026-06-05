# ======================================================================
# Dogar Trading Corporation Portal — Production Docker image
# ======================================================================
# Build:
#     docker build -t dogar-portal:latest .
#
# Run (single container, SQLite in a named volume):
#     docker run -d --name dogar-portal \
#         -p 3000:3000 \
#         -e SECRET_KEY="$(openssl rand -hex 32)" \
#         -e DEFAULT_ADMIN_PASSWORD="<strong-password-here>" \
#         -e ENV=production \
#         -v dogar-data:/app/data \
#         -v dogar-uploads:/app/app/static/uploads \
#         dogar-portal:latest
#
# Health check:
#     curl http://localhost:3000/ -I       # → HTTP 200 or 307 to /login
# ======================================================================

ARG PYTHON_VERSION=3.11-slim

# ----- Builder ---------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# System deps required to compile the few wheels that don't ship binaries
# (Pillow, reportlab, bcrypt). Removed in the final stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libfreetype6-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install --no-warn-script-location -r requirements.txt


# ----- Runtime --------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3000 \
    HOST=0.0.0.0

# Runtime-only libs (no compilers).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        libfreetype6 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user — never run a public web app as root.
RUN useradd --create-home --shell /bin/bash --uid 1000 dtc

WORKDIR /app
COPY --from=builder /install /usr/local
COPY --chown=dtc:dtc . /app

# Persisted directories — should be mounted as docker volumes in prod.
RUN mkdir -p /app/data /app/app/static/uploads /app/app/static/pdf_backgrounds \
    && chown -R dtc:dtc /app/data /app/app/static/uploads /app/app/static/pdf_backgrounds

USER dtc

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT}/ -o /dev/null || exit 1

# 2 uvicorn workers is a sane default for a 2-vCPU box; override with
# --build-arg or docker-compose for larger instances.
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT} --workers 2 --proxy-headers --forwarded-allow-ips=*"]
