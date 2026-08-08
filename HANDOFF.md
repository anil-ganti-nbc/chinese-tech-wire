# Chinese Tech Wire — Project Handoff

**Status as of handoff:** V0.5.4 (post-soak hardening + editorial observability)  
**Owner intent:** Local news-discovery / newsroom intelligence tool for a technology journalist covering Chinese hardware, semiconductors, and consumer tech.  
**Not:** an automated article writer, SaaS product, or LLM-dependent newsroom.

This document is written so another AI (or human) can continue work without the prior conversation.

---

## 1. What this project is

Chinese Tech Wire continuously (or on a schedule) ingests Chinese-language tech signals, normalizes them, clusters related items across layers, scores them for editorial value, and surfaces **Story Leads** a journalist can act on.

Long-term vision (not fully built):

```text
08:52  Chiphell   — unreleased GPU photo
09:13  JD.com     — matching model listing
09:31  BenchLife  — first publication
10:02  ITHome     — wider pickup
```

The system should preserve that chronology and answer: **what should I investigate right now, and why?**

Primary user runs it on **Windows**, locally, with SQLite. Discord optional. Gemini translation optional and non-blocking.

---

## 2. Intelligence layers (do not collapse these)

| Layer | Purpose | Package / key modules |
|-------|---------|------------------------|
| **NEWS** | Conventional publications (CN/TW/HK) | `sources/` |
| **COMMUNITY** | Hardware forums — early signals | `community_sources/` |
| **DOCUMENTARY** | Structured public records (retail, etc.) | `documentary_sources/` |
| **NEWSROOM** | Editorial StoryLeads over clusters | `pipeline/newsroom.py` |
| **GUI** | Local observational UI (FastAPI/Jinja) | `web/` |

Rules that have been enforced repeatedly:

- Do **not** represent community threads or documentary records as `Article`.
- Do **not** redesign scoring/clustering when adding UI or ops features.
- Do **not** expand sources just to fill a checklist.
- False merges are worse than missed merges.
- Translation failure must never block ingestion.
- GUI page loads must not scrape, rescore, retranslate, or recluster.

---

## 3. Version history (what “done” means)

| Version | Theme | Outcome |
|---------|--------|---------|
| **V0.1** | 5 Mainland news sources + pipeline | ITHome, MyDrivers, Expreview, ZOL, Jiwei; SQLite; score/dedup/Discord |
| **V0.2** | Taiwan/HK news | BenchLife, HKEPC, TechNews, XFastest; timezone → UTC; Traditional Chinese preserved |
| **V0.3** | Community intelligence | Chiphell, Mobile01, PTT, Coolaler; selective replies; corroboration |
| **V0.4** | Documentary intelligence | Models, snapshots, change events; **JD** retail adapter (often PARTIAL); Geekbench **removed** as active scraper |
| **V0.5** | Newsroom / StoryLeads | Editorial scoring, lifecycle, brief, explain-lead, feedback (no LLM) |
| **V0.5.1** | Local GUI | FastAPI + Jinja newsroom UI |
| **V0.5.2** | Hourly Windows automation | `--full-once`, `IngestionRun`, Task Scheduler scripts via **schtasks.exe** |

**Do not start V0.6 unless the owner asks.** Recent explicit instruction was to pause after V0.5.2 for real-world soak testing.

---

## 4. How to run (Windows)

Project path on owner machine (example):

```text
C:\Users\anil\Desktop\chinese-tech-wire
```

### Setup

```powershell
cd C:\Users\anil\Desktop\chinese-tech-wire
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# Copy .env.example → .env and fill optional keys
```

### One complete production cycle (all layers)

```powershell
python main.py --full-once
```

Runs: **news → community → documentary → StoryLead rebuild**.  
Isolates per-source failures; does not abort the whole cycle.

```powershell
python main.py --full-once --scheduled
```

Same as above but `IngestionRun.trigger = SCHEDULED` and file logging under `logs/scheduled/`.

**Important:** `python main.py --once` is **news sources only**, not a full cycle.

### GUI

```powershell
python main.py --gui
# http://127.0.0.1:8000
```

Needs: `fastapi`, `uvicorn`, `jinja2`, `python-multipart` (listed in `requirements.txt`).

### Hourly Task Scheduler

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1
powershell -ExecutionPolicy Bypass -File scripts\scheduler_status.ps1
powershell -ExecutionPolicy Bypass -File scripts\remove_scheduler.ps1
```

- Task name: `ChineseTechWire`
- Uses **schtasks.exe /SC HOURLY** (not `Register-ScheduledTask` with `[TimeSpan]::MaxValue` — that fails with `0x80041318` / `Duration:P99999999DT23H59M59S`)
- Prefers `.venv\Scripts\python.exe`
- Working directory = project root
- `MultipleInstances = IgnoreNew` after patch
- GUI does **not** need to be open

### Other useful CLI

```text
python main.py --newsroom-brief
python main.py --newsroom-brief --since-hours 8
python main.py --rebuild-leads
python main.py --explain-lead <id>
python main.py --lead-feedback <id> USEFUL|NOT_USEFUL|WRITTEN|DUPLICATE|FALSE_POSITIVE
python main.py --show-clusters
python main.py --show-community
python main.py --test-discord
python main.py --test-translation
```

---

## 5. Repository layout

```text
chinese-tech-wire/
  main.py                 # CLI entry
  config.py               # settings / env
  config/settings.yaml    # weights, watchlists, thresholds
  requirements.txt
  README.md
  HANDOFF.md              # this file
  .env / .env.example

  sources/                # NEWS adapters
  community_sources/      # COMMUNITY adapters
  documentary_sources/    # DOCUMENTARY adapters
  pipeline/               # ingest, score, dedup, newsroom, full_cycle, translate, notify
  database/               # SQLAlchemy models + db init/migration helpers
  web/                    # FastAPI GUI (app.py, templates/, static/)
  scripts/                # Windows scheduler PS1
  tests/ + tests/fixtures/
  logs/scheduled/         # scheduled run logs
  data/                   # local SQLite (often gitignored)
```

---

## 6. Active sources (registries)

### NEWS (`sources/SOURCE_REGISTRY`)

| Key | Site | Region | Notes |
|-----|------|--------|-------|
| ithome | IT之家 | CN | HTML list |
| mydrivers | 快科技 | CN | HTML list |
| expreview | 超能网 | CN | RSS |
| zol | 中关村在线 | CN | HTML |
| jiwei | 集微网 (laoyaoba) | CN | HTML |
| benchlife | BenchLife | TW | |
| hkepc | HKEPC | HK | Often fragile / fixture-heavy |
| technews | 科技新報 | TW | |
| xfastest | XFastest | TW | Soft-fail on HTTP errors |

### COMMUNITY (`community_sources/COMMUNITY_REGISTRY`)

| Key | Forum | Notes |
|-----|-------|-------|
| chiphell | Chiphell | Discuz-style; anti-bot possible |
| mobile01 | Mobile01 | |
| ptt | PTT PC_Shopping | |
| coolaler | 滄者極限 | XenForo; need canonical URL for full thread |

### DOCUMENTARY (`documentary_sources/DOCUMENTARY_REGISTRY`)

| Key | Type | Status |
|-----|------|--------|
| jd | RETAIL_LISTING | **PARTIAL** — anti-bot; soft-fails; watchlist-driven |

**Explicitly disabled:**

```text
geekbench — DIRECT MONITORING DISABLED / UNSUPPORTED
```

Permanent product decision (anti-automation + maintenance cost). Historical `source=geekbench` rows stay readable. External Geekbench URLs in posts are still **classified** as upstream evidence but **never auto-fetched**.

**Regulatory (SRRC/NCC):** investigated, **not implemented** (no stable public feed). Architecture for `REGULATORY_RECORD` / `CERTIFICATION_RECORD` remains.

---

## 7. Core data model (SQLite + SQLAlchemy)

Schema evolves with lightweight `ALTER TABLE` / `create_all` — **do not require wiping the DB** for upgrades.

Important tables:

- `articles` — news
- `story_clusters` — shared cluster; chronology fields:
  - `first_signal_source` / `first_signal_at` (community)
  - `first_documentary_source` / `first_documentary_at`
  - `first_media_source` / `first_media_at`
- `community_threads`, `community_posts`, `thread_metrics`, `author_profiles`
- `documentary_records`, `documentary_snapshots`, `documentary_events`
- `story_leads`, `lead_events`, `lead_feedback`
- `ingestion_runs` — full-cycle run history (V0.5.2)
- `translation_cache` — persistent translation cache

Timestamps: naive local Chinese times must be interpreted as **Asia/Shanghai / Taipei / Hong_Kong** then stored/compared as **UTC**. See source `parse_datetime` / timezone tests.

Article translation field name is **`title_english`** (not `title_en`).

---

## 8. Pipeline entry points (reuse these; don’t reimplement)

| Concern | Module |
|---------|--------|
| Full cycle orchestration | `pipeline/full_cycle.py` → `run_full_cycle()` |
| News run | `main.run_source` / sources adapters |
| Community run | `pipeline/community_ingest.run_community_source` |
| Documentary run | `pipeline/documentary_ingest.run_documentary_source` |
| StoryLeads | `pipeline/newsroom.py` (`rebuild_leads`, `upsert_lead_for_cluster`, `explain_lead`, `add_feedback`) |
| Dedup / title similarity | `pipeline/deduplicate.py` |
| Entities / aliases (SC+TC) | `pipeline/entities.py` + settings aliases |
| Discord | `pipeline/notify.py` |
| Translation | `pipeline/translate.py` (NoOp / OpenAI-compatible / Gemini); non-blocking |
| Scheduled file logs | `pipeline/scheduled_log.py` |
| GUI | `web.app.run_gui` |

**GUI must call existing functions** (e.g. `explain_lead`, `add_feedback`). Never invent parallel `gui_score_*` logic.

---

## 9. Newsroom scoring (V0.5) — do not retune casually

Editorial priority is a weighted combination of (configurable in `config/settings.yaml` under `newsroom.weights`):

- novelty, evidence, exclusivity, momentum, source_diversity, confidence, relevance  
- plus time decay by lead type  

Critical distinctions:

- **Exclusivity** ≠ **confidence**  
- **Media saturation** lowers exclusivity  
- Repost swarms must not inflate **source diversity**  
- High forum velocity + zero evidence must not outrank strong documentary discovery  

Lead statuses: `NEW | WATCHING | ACTIONABLE | ESCALATED | STALE | RESOLVED | DISMISSED`  
Feedback is stored for future calibration — **do not auto-train weights from feedback yet**.

Discord newsroom alerts only on **meaningful state changes**, not every score tick.

---

## 10. Config & secrets

- `config/settings.yaml` — polling, scoring weights, watchlists, newsroom thresholds, documentary settings  
- `.env` — `DISCORD_WEBHOOK_URL`, `TRANSLATION_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `DATABASE_URL`, etc.  
- **Never commit secrets.** Never log webhook URLs or API keys.  
- GUI must not display secrets.

Gemini: optional; rate limits are **DEGRADED**, not system failure. Do not spend cycles “fixing” Gemini unless asked.

---

## 11. Tests

```powershell
python -m pytest tests/ -q
```

Last known state in development environment: **~119 tests passing** (V0.2–V0.5.2).  
Fixtures under `tests/fixtures/`. Many forum/retail live paths are **FIXTURE VERIFIED** or **PARTIAL**, not LIVE VERIFIED in CI.

When adding features: run full suite; do not break news/community/documentary/newsroom regressions.

---

## 12. Known limitations (honest)

1. **JD.com** — public search often blocked; adapter soft-fails; classify PARTIAL.  
2. **Geekbench** — no direct monitoring (by design).  
3. **Forums** — anti-bot / login walls; blocked sources must not crash the cycle.  
4. **HKEPC / XFastest** — historically fragile HTTP status handling.  
5. **Clustering** — title/entity similarity; brand overlap can soft-match different model numbers; conservative thresholds preferred.  
6. **Lead rebuild** — primarily via `--rebuild-leads` / full-cycle; not every micro-event is fully event-driven.  
7. **GUI Health** — shows counts and `IngestionRun` history; not a full per-source error telemetry warehouse.  
8. **Scheduler install** — must use the **schtasks-based** `scripts/install_scheduler.ps1`; older MaxValue-based scripts fail on Windows.  
9. README still has older V0.1 research sections; treat **this HANDOFF + code** as source of truth for current behavior.

---

## 13. Explicit non-goals (unless owner reverses)

Do **not** add without a clear new brief:

- Weibo / login-gated social scraping  
- Korean / Japanese sources  
- PassMark / UserBenchmark / new benchmark scrapers as Geekbench replacements  
- OCR, image AI, embeddings, RAG, LLM ranking/summaries  
- Dashboards beyond the local FastAPI GUI  
- Redis, PostgreSQL, Kafka, Docker orchestration  
- Auto-publishing or full article generation  
- CAPTCHA/login bypass  
- Scoring weight “optimization” during soak without data  

Principle: **evidence that changes what a journalist knows > volume of records.**

---

## 14. Suggested next work (only if owner requests)

In rough priority for a post–soak-test phase:

1. Analyze soak metrics (`IngestionRun`, `LeadFeedback`, Discord noise)  
2. Fix genuine defects found in multi-day runs (parsers, false clusters, alert spam)  
3. Event-driven lead refresh hooks after ingest (without changing formulas)  
4. Stronger health/telemetry (last success/fail per source)  
5. One high-value new source **only if** public access is reliable  
6. Optional V0.6 product brief — **do not invent scope**

---

## 15. Working style preferences (from owner)

- Production-minded but **lightweight**; runnable and debuggable locally  
- Prefer simplest expandable design over architectural perfection  
- Inspect live sites before scrapers; RSS/API first, HTML second, browser automation last resort  
- Sequential implementation; don’t stop after every sub-task asking for permission when the brief says to continue  
- Report LIVE vs FIXTURE vs PARTIAL vs BLOCKED honestly  
- When Windows scheduling or ops breaks, fix the script — don’t redesign intelligence  
- Owner may conserve AI usage limits; prefer complete, correct deliverables over chatty intermediate steps  

---

## 16. Quick “are we healthy?” checklist

```powershell
python main.py --full-once --dry-run
python main.py --rebuild-leads --dry-run
python main.py --newsroom-brief
python main.py --gui
# Health page: http://127.0.0.1:8000/health
powershell -ExecutionPolicy Bypass -File scripts\scheduler_status.ps1
python -m pytest tests/ -q
```

---

## 17. File to give the next AI

Hand them:

1. This **`HANDOFF.md`**
2. The project tree (or latest zip, e.g. `chinese-tech-wire-v0.5.2.zip`)
3. Optional: recent soak notes / Discord noise examples / failed scheduler logs

Opening prompt suggestion for the next session:

> Read HANDOFF.md and the repo. Current baseline is V0.5.2. Do not start V0.6 or change scoring unless I explicitly ask. First confirm you understand the four intelligence layers and how `--full-once` differs from `--once`. Then wait for my task.

---

*End of handoff.*


---

## 18. V0.5.3 Discord / StoryLead alert stabilization (post-soak)

### Root cause (soak evidence)

- ~3,121 StoryLeads; max priority ≈ **56.8**
- ACTIONABLE count = **0** (lifecycle threshold 70 unreachable)
- Alert path required `ACTIONABLE|ESCALATED` **and** score ≥ 75 → **zero** eligible alerts
- Legacy article Discord path was opt-in/disabled → total silence
- `_maybe_newsroom_alert` marked `notified=True` even when `send_discord` returned False

### Fix summary

- Alert eligibility **separated** from lifecycle ACTIONABLE semantics
- Config under `newsroom.alerts` (min_priority 52, allowed WATCHING+, quality gates, max_age_hours 48)
- Backlog shield via `policy_activated_at` / `POLICY_ACTIVATED` ledger marker
- `DiscordSendResult` structured delivery result
- `lead_notifications` ledger table
- Full-cycle alert counters on `IngestionRun`
- CLI: `--diagnose-alerts`, `--preview-alerts`, `--test-storylead-discord`, `--show-notifications`
- `notified` only set after confirmed successful send

### Operator commands

```powershell
python main.py --diagnose-alerts
python main.py --preview-alerts --since-hours 24 --limit 25
python main.py --test-discord
python main.py --full-once
python main.py --show-notifications --limit 20
```

Rollback: set `newsroom.alerts.enabled: false` in `config/settings.yaml`.


---

## 19. V0.5.4 Post-soak hardening

### Verified V0.5.3 baseline (against code + tests, not HANDOFF alone)

- LeadNotification ORM model present
- Safe ALTER migrations for last_notified_priority + ingestion alert counters
- notified=True only after DiscordSendResult.sent
- Backlog shield via POLICY_ACTIVATED / policy_activated_at
- preview-alerts does not POST
- 150 tests green after V0.5.4

### New operator commands

```powershell
python main.py --diagnose-alerts
python main.py --source-health
python main.py --feedback-report
python main.py --preview-alerts --since-hours 24 --limit 25
python main.py --show-notifications --limit 25
```

### GUI

- `/notifications` — alert ledger
- `/runs/{id}` — ingestion run detail
- `/health` — source-health table

### Deferred (V0.6 candidates, not implemented)

- Scoring weight retune from feedback
- Event-driven lead rebuild hooks
- Weibo / retail expansion
- Per-source GUI live probes
