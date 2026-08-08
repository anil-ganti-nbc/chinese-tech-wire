# Chinese Tech Wire — STAGING / SOAK image ONLY (Tier B clank).
# Do not treat this as, or label this, a production deployment. No
# production compose file exists for this clank in this phase; see
# ai/handoff/DECISIONS.md.
FROM python:3.12-slim-bookworm

LABEL clank.id="chinese-tech-wire"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite:////app/data/ctw.db \
    CTW_RELEASE_CHANNEL=soaking

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ctw

# Dependencies first for layer caching. requirements.txt only — NOT
# requirements-build.txt, which is PyInstaller-only and irrelevant to the
# container path (dist/ChineseTechWire.exe is a separate distribution).
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Application code. Deliberately excludes tests/, ai/, build/, dist/, and
# the PyInstaller launcher (launcher_main.py, ChineseTechWire.spec) — none
# of that is part of the container runtime surface.
COPY main.py config.py runtime_bridge.py ./
COPY sources ./sources
COPY community_sources ./community_sources
COPY documentary_sources ./documentary_sources
COPY database ./database
COPY pipeline ./pipeline
COPY web ./web
COPY config ./config
COPY scripts ./scripts

RUN mkdir -p /app/data \
    && chmod +x /app/scripts/entrypoint.sh \
    && chown -R ctw:ctw /app

USER ctw

# No GUI/browser auto-open: --gui calls webbrowser.open, which has no
# meaning in a headless container. The default CMD below is a one-shot
# ingestion cycle, never --gui.
HEALTHCHECK --interval=60s --timeout=20s --start-period=15s --retries=3 \
    CMD ["python", "main.py", "--health"]

ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
CMD ["--full-once"]
