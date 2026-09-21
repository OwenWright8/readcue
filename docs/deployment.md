# Deploying readcue

readcue is one container: the web UI, the summary worker and the reminder scheduler run in the same process, with
SQLite for storage. That keeps it simple, and it means **you run exactly one instance**.

## Install

Requires Docker with Compose v2. Nothing here needs a recent version, so an older NAS Docker is fine.

**The published image** (amd64 and arm64) is the normal way. It needs only two files and no source code:

```bash
mkdir readcue && cd readcue
curl -O https://raw.githubusercontent.com/OwenWright8/readcue/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/OwenWright8/readcue/main/.env.example
# edit .env: your API key, Pushover keys, timezone, ...
docker compose up -d
```

Pin a release for repeatable installs with `READCUE_VERSION=1.0.0` in `.env` (default: `latest`).

**Build from source** (development, an unusual CPU, or extra OCR languages):

```bash
git clone https://github.com/OwenWright8/readcue.git && cd readcue
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## On a NAS (Synology, QNAP, Unraid, TrueNAS, Portainer)

You need only `docker-compose.yml` and a `.env`. Do **not** use the build override: a NAS has no source checkout,
which is what `failed to read dockerfile: open Dockerfile: no such file or directory` means. Every setting is passed
through the compose file's `environment:` block, so this also works when your NAS UI gives you a compose editor and an
environment-variables panel instead of a `.env` file.

1. **Put the two files in a folder** on the NAS (e.g. `/volume1/docker/readcue`), or paste the compose file into the
   Compose/Projects/Stacks screen and enter the variables from `.env.example` in its environment panel.
2. **Make the image pullable.** The image is at `ghcr.io/owenwright8/readcue`; the package must be set to *public*
   (GitHub: your profile → Packages → readcue → Package settings → Change visibility), or you must `docker login ghcr.io`
   on the NAS with a token that has `read:packages`.
3. **Make it reachable from your LAN.** The app listens on localhost by default, so other devices would see nothing.
   Set `READCUE_BIND=0.0.0.0` **and a password** (the app refuses to start on the network without one):

   ```env
   READCUE_BIND=0.0.0.0
   READCUE_PASSWORD=choose-something-long
   READCUE_BASE_URL=http://nas.local:8080     # your NAS's address; used for links in notifications
   TZ=America/New_York
   ```

4. **Keep the data in a shared folder (optional).** By default the data lives in a Docker-managed volume. To use a folder
   you can browse and back up, point `READCUE_DATA` at it and run the container as that folder's owner:

   ```env
   READCUE_DATA=/volume1/docker/readcue/data
   READCUE_UID=1026      # from `id <your-user>` over SSH
   READCUE_GID=100
   ```

   Create the folder first. Without a matching `READCUE_UID`/`READCUE_GID` you'll see "unable to open database file".
5. `docker compose up -d`, then open `http://<nas-address>:8080`.

The bundled Ollama container runs on the CPU unless your NAS has a supported GPU, which is slow for long chapters.
The Claude API, or an Ollama on a faster machine (`OLLAMA_HOST=http://<that-machine>:11434`), works better.

## Health

Check it's healthy: `docker compose ps` should say `healthy`, and `curl localhost:8080/healthz` returns
`{"status":"ok",...}`.

## Configuration reference

Everything is an environment variable, normally set in `.env`.

| Variable | Default | Meaning |
|---|---|---|
| `READCUE_LLM_PROVIDER` | `anthropic` | `anthropic` (Claude API) or `ollama` (self-hosted). |
| `ANTHROPIC_API_KEY` | | Claude API key. |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Any Claude model ID. |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Output cap per request. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama address (see `.env.example` for the Docker cases). |
| `OLLAMA_MODEL` | `llama3.1:8b` | Model to use. |
| `OLLAMA_NUM_CTX` | `16384` | Context window in tokens; longer chapters are summarized in parts. |
| `OLLAMA_TIMEOUT` | `900` | Seconds to wait for a response. |
| `OLLAMA_API_KEY` | | Bearer token, if your Ollama sits behind an authenticating proxy. |
| `PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY` | | Both are needed for notifications. |
| `PUSHOVER_DEVICE` | all | Default device name(s), comma-separated, used until you choose in Settings → Choose devices (which overrides it). |
| `PUSHOVER_PRIORITY` | `0` | `-2` to `1`. |
| `READCUE_SUMMARIZE_DAYS_BEFORE` | `3` | Scheduled summaries are written this many days before the due date. |
| `READCUE_NOTIFY_DAYS_BEFORE` | `1` | The reminder goes out this many days before the due date. |
| `READCUE_NOTIFY_TIME` | `08:00` | ...from this local time. |
| `READCUE_CHECK_INTERVAL` | `60` | Seconds between reminder checks. |
| `READCUE_MAX_UPLOAD_MB` | `1024` | Largest upload per request; a whole scanned textbook can be several hundred MB. Raise your proxy's limit to match. |
| `READCUE_OCR_LANG` | `eng` | Tesseract language(s), e.g. `eng+spa`. The published image has English only; others need a source build with `EXTRA_OCR_PACKAGES`. |
| `TZ` | `UTC` | Your timezone, e.g. `America/New_York`. Reminder times and due dates use it. |
| `READCUE_BASE_URL` | | Public address, used for links in notifications and to accept a reverse proxy's Origin. |
| `READCUE_BIND` | `127.0.0.1` | Address the port is published on. |
| `READCUE_PORT` | `8080` | Host port. |
| `READCUE_USERNAME`, `READCUE_PASSWORD` | `readcue`, none | HTTP basic auth. |
| `READCUE_INSECURE_NO_AUTH` | `false` | Allow a network-reachable app with no password. Don't. |
| `READCUE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `READCUE_DATA_DIR` | `/data` | Where the database lives inside the container. Set by the image; don't change it. |
| `READCUE_DATA` | `readcue-data` | What is mounted at `/data`: a Docker volume name, or an absolute folder path. |
| `READCUE_UID`, `READCUE_GID` | `1000`, `1000` | User the container runs as. Set them to the owner of `READCUE_DATA` when it's a folder. |
| `COMPOSE_PROFILES` | | Set to `ollama` to run the bundled Ollama container. |
| `EXTRA_OCR_PACKAGES` | | Extra apt packages for OCR languages, e.g. `tesseract-ocr-spa`. Build-time. |
| `READCUE_VERSION` | `latest` | Image tag to pull, e.g. `1.0.0`. |

## Security

What the app does for you:

- **Won't start exposed without a login.** If `READCUE_BIND` isn't localhost and `READCUE_PASSWORD` is empty, it exits with an
  explanation (override with `READCUE_INSECURE_NO_AUTH=true` if you really mean it).
- Basic auth (constant-time comparison), cross-site POST refusal, a strict Content-Security-Policy (no inline
  script), `X-Frame-Options: DENY`, `Cache-Control: no-store` on pages.
- Runs as a non-root user with all Linux capabilities dropped and `no-new-privileges`.
- API keys and tokens are never written to logs.

What you should do:

- Keep it on localhost, a LAN or a VPN (Tailscale/WireGuard). If it must be on the internet, use **HTTPS** and a strong
  password, because basic auth over plain HTTP sends the password in the clear.
- Remember that with the Claude provider, chapter text is sent to Anthropic to be summarized. Use Ollama if that's not OK.
- Textbook text and summaries are stored unencrypted in the data volume or folder. Use full-disk encryption on the host.

## HTTPS with a reverse proxy

Set `READCUE_BIND=127.0.0.1` (the default), `READCUE_PASSWORD`, and `READCUE_BASE_URL=https://readcue.example.com`, then
proxy to `127.0.0.1:8080`. Uploads (especially OCR'd scans) can take minutes, so raise the timeouts.

**Caddy** (automatic HTTPS):

```
readcue.example.com {
    request_body {
        max_size 1GB
    }
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            response_header_timeout 15m
        }
    }
}
```

**nginx:**

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 1g;
    proxy_read_timeout 900s;
}
```

`READCUE_BASE_URL` matters here: it tells the app that requests whose `Origin` is your public address are legitimate,
which stays true even if your proxy rewrites the `Host` header.

## Backups

The data is one SQLite file. Don't copy it with `docker cp` while the app runs; use the built-in backup, which
is consistent even mid-write:

```bash
docker compose exec readcue readcue backup                    # writes /data/backups/readcue-<timestamp>.db, keeps the newest 14
docker compose exec readcue readcue backup --to /data/pre-upgrade.db
docker compose cp readcue:/data/backups ./readcue-backups     # copy them off the volume
```

Run it on a schedule with cron on the host:

```
0 3 * * *  cd /path/to/readcue && docker compose exec -T readcue readcue backup >/dev/null
```

**Restore:**

```bash
docker compose stop readcue
docker compose cp ./readcue-backups/readcue-20260920-030000.db readcue:/data/readcue.db
docker compose start readcue
```

## Upgrading

```bash
docker compose pull && docker compose up -d                                   # published image
git pull && docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build   # from source
```

Database changes are applied automatically at startup. Take a backup first, and read [CHANGELOG.md](../CHANGELOG.md).

## Monitoring

- `GET /healthz` returns 200 with `{"status":"ok","version":...}`, or 503 listing what's wrong (database unreachable,
  or the summary worker / reminder scheduler thread has stopped). It needs no login, so uptime monitors can use it.
- `docker compose logs -f readcue` shows every summary, reminder and error. Logs rotate at 10 MB x 3 files.
- Set `READCUE_LOG_LEVEL=DEBUG` when chasing a problem.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `failed to read dockerfile: open Dockerfile: no such file or directory` | You're using the build override (or an old compose file) without the source. Use plain `docker-compose.yml` + `.env`; see "On a NAS". |
| `denied` / `unauthorized` pulling the image | The GHCR package is private: make it public, or `docker login ghcr.io`. |
| "unable to open database file" | The data folder isn't writable by the container's user: set `READCUE_UID`/`READCUE_GID` to its owner. |
| Container exits: "reachable on ... with no login" | Set `READCUE_PASSWORD`, or `READCUE_BIND=127.0.0.1`. |
| Summaries fail: "No Claude API credentials found" | Set `ANTHROPIC_API_KEY` in `.env`, then `docker compose up -d`. |
| Summaries fail: "Couldn't reach Ollama" | Check `OLLAMA_HOST` (see the three cases in `.env.example`); pull the model with `ollama pull`. |
| No notifications | Settings shows whether Pushover is configured; use **Send test notification**. |
| Forms return "403 Forbidden" behind a proxy | Set `READCUE_BASE_URL` to the public address. |
| "That upload is too large" | Raise `READCUE_MAX_UPLOAD_MB`, and your proxy's body-size limit (nginx `client_max_body_size`). |
| A textbook stays on "Reading…" | Scanned pages take a few seconds each; check `docker compose logs`. It resumes after a restart. |
| Upload times out on a scanned book | Give a page range; raise the proxy's read timeout. OCR is capped at 150 pages per upload. |
| "OCR isn't installed" | Only happens outside the Docker image; install `tesseract-ocr` and `poppler-utils`. |
| Reminders at the wrong hour | Set `TZ` (and `READCUE_NOTIFY_TIME`). |
| Everything looks stuck | Only one instance may run. Check `docker compose ps`, and `/healthz`. |
