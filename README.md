# Chinese Tech Wire — V0.1

> **Phase 0: UNVERIFIED_PRODUCTION — promotion frozen.** See
> [`PHASE0_CONTAINMENT.md`](PHASE0_CONTAINMENT.md); historical secret scanning
> and credential rotation remain operator-required before release.

> Status: Staging / additional soak testing required

Lightweight local news discovery tool that monitors major Chinese technology sites, normalizes metadata, scores stories for a hardware/semiconductor journalist, clusters duplicates, and pushes high-value leads to Discord.

**Not an automated writer.** Goal: surface potentially newsworthy Chinese hardware / semiconductor / consumer-tech stories before or shortly after they hit English media.

## Pre-implementation Research (Phase 0)

### 1. IT之家 / ITHome — https://www.ithome.com/
- **Best discovery**: Listing page https://www.ithome.com/list/ (clean chronological list).
- **URL pattern**: `https://www.ithome.com/0/{section}/{id}.htm` → source_article_id = the numeric id.
- **RSS**: Historical `/rss/` now appears to render as HTML list of titles rather than a clean XML feed. Prefer HTML parse.
- **Fragility**: Dynamic “load more”; possible future JS changes. Parser uses link pattern + nearby timestamps → reasonably robust. No Playwright needed for V0.1.
- **Status**: Implemented first (PHASE 2).

### 2. 快科技 / MyDrivers — https://news.mydrivers.com/
- **Best discovery**: Listing page `https://news.mydrivers.com/` (latest section). RSS exists at `https://rss.mydrivers.com/Rss.aspx?Tid=1` but listing proved more reliable for clean structure.
- **URL pattern**: `https://news.mydrivers.com/1/{channel}/{id}.htm` → numeric id.
- **Fragility**: Possible AJAX loading; parser uses resilient link-pattern + nearby timestamps (same style as ITHome). Rate-limit carefully.
- **Status**: ✅ Implemented (HTML listing + fixture tests).

### 3. 超能网 / Expreview — https://www.expreview.com/
- **Best discovery**: Working RSS feed at https://www.expreview.com/rss.php (title, link, description, category, author, pubDate).
- **Fragility**: Low while RSS remains stable. Short timezone offsets (`+08`) normalized to `+0800`.
- **Status**: ✅ Implemented (RSS parser + fixture tests).

### 4. 中关村在线 / ZOL — https://www.zol.com.cn/
- **Best discovery**: News center `https://news.zol.com.cn/`.
- **URL pattern**: `https://{sub}.zol.com.cn/{yymm}/{id}.html` → numeric id.
- **Fragility**: Multi-subdomain, mixed content; parser uses resilient link-pattern matching. Timestamps sometimes sparse on listing.
- **Status**: ✅ Implemented (listing + fixture tests).

### 5. 集微网 / Jiwei (laoyaoba) — https://www.laoyaoba.com/
- **Best discovery**: Homepage `https://www.laoyaoba.com/`.
- **URL pattern**: `/n/{id}`.
- **Timestamps**: Relative (“41分钟前”, “5小时前”) or short absolute; dedicated relative-time parser converts to UTC.
- **Fragility**: Relative times are clock-dependent; semiconductor focus is high-value for this tool.
- **Status**: ✅ Implemented (listing + relative-time + fixture tests).

**Architecture deviations from original sketch**: None material. Added `config/settings.yaml` for human-editable weights/keywords (as required). Shared HTTP + rate-limit lives in `sources/base.py`. No Redis/Celery/Docker.

## Quick Start

```bash
cd chinese-tech-wire
python -m venv .venv
source .venv/bin/activate   # or Windows equivalent
pip install -r requirements.txt
cp .env.example .env
# Edit .env — at minimum set DISCORD_WEBHOOK_URL if you want alerts
python main.py --once          # full cycle of all 5 sources
python main.py --source jiwei --dry-run
python main.py --source zol
python main.py --source mydrivers
python main.py --show-recent
python main.py                 # continuous (default 3–7 min per source)
```

SQLite DB lands in `data/ctw.db`.

## Current Phase Status

- **PHASE 1** ✅ Project skeleton, config, models, logging, base interface, DB
- **PHASE 2** ✅ ITHome — fetch → parse → normalize → score → DB (+ optional Discord)
- **PHASE 4** ✅ All five V0.1 sources implemented + fixture tests (ITHome, Expreview, MyDrivers, ZOL, Jiwei)
- Remaining polish: richer clustering, health warnings, live soak testing

## Design Principles Followed

Reliability > cleverness · HTTP before browser · Deterministic before LLM · Preserve original Chinese · Store everything, notify selectively · Isolate source failures · Config outside code.

## Future (not in V0.1)

Weibo leaker watcher, JD/Tmall retail SKU monitor, OEM sites, regulatory DBs, benchmark DBs, cross-correlation timeline (“first detected → confirmation → media pickup”).


## V0.4 Documentary Intelligence

Third layer alongside NEWS and COMMUNITY.

### Active documentary sources
- **JD.com** — retail watchlist monitoring (often PARTIAL due to anti-bot)

### Disabled
- **Geekbench — DIRECT MONITORING: DISABLED / UNSUPPORTED**

Direct automated Geekbench monitoring is intentionally not supported due to
access/anti-automation constraints and maintenance cost. This is a permanent
product decision unless explicitly reversed.

Historical `source=geekbench` DocumentaryRecords remain readable and participate
in story chronology. External Geekbench URLs found in news or community posts
are still classified as upstream evidence but are **never fetched automatically**.

Generic `BENCHMARK_RECORD` support remains for indirect evidence and future
publicly accessible benchmark sources.


### Regulatory / certification (investigated, not implemented)

| Authority | Record types | Public access | Status |
|-----------|--------------|---------------|--------|
| MIIT SRRC (无线电型号核准) | radio/wireless type approval | Multi-step government portals; no stable open listing API | **BLOCKED** for automated monitoring |
| NCC Taiwan | telecom equipment | Complex public query UX | **BLOCKED** / not automatable in V0.4 |

No regulatory adapter shipped. Architecture (REGULATORY_RECORD / CERTIFICATION_RECORD scoring) remains for future sources.


## V0.5.2 Hourly Windows automation

One complete production cycle (all layers):

```bash
python main.py --full-once
python main.py --full-once --scheduled   # used by Task Scheduler
```

Note: `python main.py --once` still runs **news sources only**.

### Install hourly Task Scheduler job (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_scheduler.ps1
```

Status:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/scheduler_status.ps1
```

Remove:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/remove_scheduler.ps1
```

- Task name: `ChineseTechWire`
- Interval: 60 minutes
- Non-overlap: `MultipleInstances = IgnoreNew`
- Prefers `.venv\Scripts\python.exe`
- Working directory: project root
- Logs: `logs/scheduled/ctw-scheduled.log` (rotated ~21 days)
- Run history: `IngestionRun` table (visible on GUI Health page)

GUI does not need to be open for scheduled runs.

## V0.5.6.1 Dashboard launcher (Windows .exe)

Easiest way to open the dashboard: double-click **`ChineseTechWire.exe`**
(build output lands in `dist/` — copy it into the project root, alongside
`data/`, `config/`, `.env`, and `logs/`, and launch it from there).

It:
- picks a free localhost port automatically (tries `18760`–`18799` first,
  then falls back to any free OS-assigned port) — **the port differs
  between launches**, so don't bookmark a fixed URL;
- opens your default browser to the dashboard once the server is actually
  ready (never before);
- reuses an already-running CTW dashboard instead of starting a duplicate
  if you double-click it again;
- reads and writes the *same* `data/ctw.db` / `config/settings.yaml` /
  `.env` as `python main.py` — it never creates an isolated copy;
- never touches the hourly scheduled collector (`--full-once --scheduled`)
  — it's purely a viewer for the same database.

Its active `host:port` is written to `data/dashboard_runtime.json` while
running (deleted on clean exit). Startup/shutdown/errors are logged to
`logs/dashboard-launcher.log` — never Discord webhooks, API keys, or
`.env` contents.

If it can't find `config/settings.yaml` next to itself, it fails with a
visible error rather than silently creating a new empty database — this
means the `.exe` is not sitting in the real project root.

Command-line equivalent (also gets the dynamic-port behavior):

```bash
python main.py --gui --gui-port auto --gui-open-browser
```

Plain `python main.py --gui` still works exactly as before (fixed port
8000, no auto-open) — nothing about existing invocations changed.

### Building the .exe

```bash
pip install -r requirements-build.txt
.venv\Scripts\pyinstaller.exe ChineseTechWire.spec --noconfirm
```

Output: `dist/ChineseTechWire.exe`. Only `web/templates` and `web/static`
(application code) are bundled into it — `data/`, `config/`, and `.env`
are never bundled and must exist in the folder where you run the `.exe`.
