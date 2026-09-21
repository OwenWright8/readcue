# readcue

[![CI](https://github.com/OwenWright8/readcue/actions/workflows/ci.yml/badge.svg)](https://github.com/OwenWright8/readcue/actions/workflows/ci.yml)

Self-hosted reading assistant for a class. Give it your syllabus and feed it your textbook chapter by
chapter. It summarizes each one (overview, key points, and important definitions), pings you on Pushover
when a summary is complete, and reminds you the day before the chapter is due.

```
syllabus  ──►  AI finds every chapter + due date  ──►  you review them and choose:
                                                       ● Summarize now   or   ● Schedule the summaries
chapter 4 ──►  text read (OCR if it's a scan)     ──►  stored
10/15     ──►  Claude writes the summary (scheduled: 3 days before it's due)
              Pushover: "Summary ready: Ch. 4 — BIO 101"                       [Open summary]
10/17 08:00 ─►  Pushover: "Read Ch. 4 (due tomorrow) — BIO 101. Summary is ready."
```

Summaries are written by **Claude Sonnet** by default. A **self-hosted Ollama** model works too; switch
with one setting.

## Quick start

Requires Docker with Compose v2. You need only two files, with no source code:

```bash
mkdir readcue && cd readcue
curl -O https://raw.githubusercontent.com/OwenWright8/readcue/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/OwenWright8/readcue/main/.env.example
# edit .env: add ANTHROPIC_API_KEY and your Pushover keys
docker compose up -d
```

Open <http://localhost:8080>. For a **NAS**, building from source, or HTTPS, see
[docs/deployment.md](docs/deployment.md).

1. **Add a course** and **import its syllabus** (PDF, DOCX, TXT/MD, scans or photos, or pasted text). The
   AI pulls out the chapter readings and due dates. You review and edit them, then choose whether
   summaries are **written now** or **scheduled** (see below), and whether to be notified when each is done.
2. **Add chapters** whenever you have them, even weeks early: upload a PDF/DOCX/TXT, scanned pages, photos,
   or paste text. For a whole-book PDF, give a page range like `80-112`.
3. That's it. The dashboard shows every chapter's progress (chapter added, summary, reminder) and the next
   thing to do for it. **Settings** has buttons to test the AI connection and send a test notification.

## Reading scans and photos (OCR)

The Docker image includes Tesseract and Poppler, so nothing needs configuring for English.

- **Scanned PDFs**: pages with no text layer are OCR'd; pages that already have text are left alone, so
  mixed PDFs work. At most 150 pages are OCR'd per upload, so for a scanned book use a page range.
- **Photos of pages** (PNG, JPG, TIFF, BMP): upload several at once and they're joined in filename order
  (`IMG_2` before `IMG_10`). HEIC from an iPhone needs converting to JPG first.
- **Other languages**: set `READCUE_OCR_LANG=spa` (or `eng+spa`) and add the language data to the image
  with `EXTRA_OCR_PACKAGES="tesseract-ocr-spa"` in `.env`, then `docker compose build`.
- OCR runs during the upload, so a long scan keeps the page busy for a while (roughly a few seconds per
  page). If you put the app behind a reverse proxy, raise its read timeout (nginx: `proxy_read_timeout 600s`).

Recognition quality depends on the scan. Skim a summary of a poor scan before trusting it.

## Summarize now, or schedule it

Once the syllabus has been read you choose how summaries are written. It's a per-course setting, and you can
change it any time from the sliders button on the course card.

| | **Schedule them** (default) | **Summarize now** |
|---|---|---|
| When | Each summary is written `READCUE_SUMMARIZE_DAYS_BEFORE` (3) days before its chapter is due | As soon as you add the chapter |
| Good for | Semesters where the schedule might shift: nothing is spent on a chapter until it's close | A final schedule, or reading ahead |

Either way, a single chapter can be pushed through early with **Summarize now** on its row.

**Notify me when a summary is complete** (on by default) sends a Pushover ping with the summary's gist and an
**Open summary** link the moment one finishes. That matters most for scheduled summaries, which land days before
you'd otherwise look. To avoid double pings you won't get one when the day-before reminder is about to say the
same thing, or if you've already been told about that chapter.

## When things happen

| Setting | Default | Meaning |
|---|---|---|
| `READCUE_SUMMARIZE_DAYS_BEFORE` | `3` | A chapter is summarized only once it's due within this many days. |
| `READCUE_NOTIFY_DAYS_BEFORE` | `1` | The reminder goes out this many days before the due date (10/17 for 10/18). |
| `READCUE_NOTIFY_TIME` | `08:00` | ...from this local time (set `TZ` to your timezone). |

- **Why schedule summaries?** Syllabi change. Holding summaries back until a chapter is close means an
  upload made in week 1 costs nothing until its date is near, and a moved due date never wastes a summary.
  Uploaded chapters show "Starts Thu Oct 15" on the dashboard. **Summarize now** overrides the wait for
  a chapter you want to read early, and **Regenerate/Retry** always run immediately. A course set to
  *Summarize now* skips the wait entirely.
- A chapter uploaded *after* its window has opened (or already past due) is summarized right away.
- A chapter that isn't on the schedule is stored but never summarized automatically; add its due date.
- If a chapter hasn't been uploaded when its reminder is due, you get a "no summary yet" notice, and a
  second "summary is ready" one once it exists. Never more than two per chapter.
- If the app was down on the reminder day, the reminder still fires up to and including the due date.
- Changing a chapter's due date re-arms its reminder. Past-due chapters are never announced, so importing
  a syllabus mid-semester doesn't produce a burst of stale pings.
- Keep `NOTIFY_DAYS_BEFORE` at or below `SUMMARIZE_DAYS_BEFORE` so the summary exists when the reminder
  arrives. The app warns at startup if not.

## Configuration

Everything is an environment variable in `.env` (see [.env.example](.env.example) for the full list).

### Choose your AI

**Claude (default)**

```env
READCUE_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-5
```

**Ollama, bundled container**

```env
READCUE_LLM_PROVIDER=ollama
OLLAMA_HOST=http://ollama:11434
OLLAMA_MODEL=llama3.1:8b
COMPOSE_PROFILES=ollama
```

```bash
docker compose up -d
docker compose exec ollama ollama pull llama3.1:8b
```

**Ollama already running elsewhere:** point `OLLAMA_HOST` at it (`http://host.docker.internal:11434` for
the machine running Docker, or `http://<lan-ip>:11434`) and leave `COMPOSE_PROFILES` unset.

Small local models are fine for summaries but can misread messy syllabus tables. That's why the schedule
is always shown for review before saving. Chapters too long for a model's context window
(`OLLAMA_NUM_CTX` for Ollama; roughly half of it is usable for text) are summarized in parts and combined;
definitions from every part are kept.

### Pushover

Create an application at <https://pushover.net/apps/build> for `PUSHOVER_APP_TOKEN`; your user key is on
the Pushover dashboard (`PUSHOVER_USER_KEY`).

Set `READCUE_BASE_URL` (for example `http://my-server.local:8080`) so notifications carry an **Open summary**
link. It needs to be reachable from your phone (LAN, VPN or Tailscale).

### Access and security

The web UI listens on `127.0.0.1` only. To reach it from other devices set `READCUE_BIND=0.0.0.0` **and**
`READCUE_PASSWORD` (HTTP basic auth, username `readcue`); **the app refuses to start** if it would be reachable
beyond localhost without a password. It has no TLS of its own, so put it behind a reverse proxy or VPN if it leaves
your LAN (Caddy and nginx examples are in [docs/deployment.md](docs/deployment.md)). Textbook text and summaries
live in the `readcue-data` Docker volume. With the Claude provider, chapter text is sent to the Anthropic API to be
summarized; use Ollama if that's not acceptable for your material.

## Operations

```bash
docker compose logs -f readcue                      # watch summaries and reminders
docker compose exec readcue readcue check --dry-run --today 2026-10-17    # what would be sent that day?
docker compose exec readcue readcue check --today 2026-10-17              # actually send it
docker compose down                                 # stop (data is kept)
docker compose down -v                              # stop and DELETE all data
```

Back up the database (a single SQLite file). Use the built-in command, which is safe while the app is running:

```bash
docker compose exec readcue readcue backup       # writes /data/backups/readcue-<timestamp>.db
docker compose cp readcue:/data/backups ./readcue-backups
```

Run one instance only: the web app, summarizer and reminder scheduler share a process. Health is at `/healthz`.
Backups, upgrades, monitoring and troubleshooting are covered in [docs/deployment.md](docs/deployment.md).

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest
READCUE_DATA_DIR=./data readcue serve      # reads ./.env if present
```

CI runs the same checks on Python 3.11, 3.13 and 3.14 with real Tesseract installed, then builds the Docker image and
smoke-tests the running container. To cut a release: bump `__version__` in `src/readcue/__init__.py`, add a
`## [x.y.z]` entry to `CHANGELOG.md`, and push a `vx.y.z` tag. The release workflow verifies the tag matches the
version, publishes a multi-arch image to GHCR, and creates the GitHub Release from the changelog entry.

To use OCR outside Docker, install `tesseract-ocr` and `poppler-utils` (macOS: `brew install tesseract
poppler`). Without them the app still runs and reads text files and text-based PDFs.

Layout: `web.py` + `templates/` + `static/` (the UI: plain Flask and hand-written CSS with light and dark
themes, no build step or external assets), `worker.py` (summary queue and the 3-day gate), `scheduler.py`
(reminders and completion pings),
`extract.py` / `ocr.py` (reading files), `summarize.py` / `syllabus.py` (prompting), `llm/` (Claude and
Ollama providers), `notify.py` (Pushover), `db.py` (SQLite).

## Limits

- Only textbook *chapter* readings are scheduled (integer chapter numbers). Articles, exams and
  assignments are ignored.
- EPUB isn't supported; export chapters to PDF or text.
- One instance, one SQLite file: it's built for a person or a household, not for many concurrent users.

## License

[MIT](LICENSE)
