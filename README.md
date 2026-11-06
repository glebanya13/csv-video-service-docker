# Video Web Service in Docker (Apache + mod_wsgi + PostgreSQL)

**Internship Task 1** reworks the Test Task 2 solution so that the video web service and its
database run in two separate Docker containers, started together with Docker Compose:

| Container | Image | What runs inside |
|---|---|---|
| `web` | built from `Dockerfile` | Apache 2.4 + mod_wsgi (daemon mode) serving the Flask app `web_service.py` |
| `db` | `postgres:16` | PostgreSQL; replaces SQLite from Test Task 2 |

```
 host :8000 ──► web (Apache :80) ──┬── /media/*  → files in the uploads volume (served by Apache)
                                   └── /*        → mod_wsgi → Flask (web_service.py) ──► db :5432
                                                                                          (db_data volume)
```

The recorder from Test Tasks 1–2 (`capture_video.py`, `qscript.py`, `video_client.py`) runs on
the host, as before, and talks to the container at `http://127.0.0.1:8000`.

## Quick start

Requirements: Docker with Docker Compose v2.

```bash
cp .env.example .env        # optional: set your own DB password (defaults are for local dev)
docker compose up --build
```

`web` starts only after `db` reports healthy, waits until PostgreSQL accepts connections,
creates the `videos` table and then starts Apache. The service is at `http://127.0.0.1:8000`.

```bash
curl http://127.0.0.1:8000/health
{"status":"ok"}

curl -F "file=@video/Acme Retail Ltd.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}
```

Record, upload and preview with the recorder on the host (as in Test Task 2):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-recorder.txt
python -m playwright install chromium
python capture_video.py --server http://127.0.0.1:8000
```

Useful commands:

```bash
docker compose ps                                  # both services, health status
docker compose logs -f web                         # Apache access/error log + app log
docker compose exec web curl http://localhost/health
docker compose exec db psql -U video_user -d videos -c "SELECT company_name, video_url, updated_at FROM videos"
docker compose down                                # stop; data and videos are kept
docker compose down -v                             # stop and DELETE the volumes
```

## Files

| File | Task | Contents |
|---|---|---|
| `Dockerfile` | new | Two-stage build of the `web` image: compiles `mod_wsgi` and `psycopg2` for the image's Python 3.12, then installs Apache, the wheels and the app |
| `docker-compose.yml` | new | Services `web` and `db`, volumes `db_data` and `uploads`, healthcheck, `depends_on: service_healthy` |
| `apache/video-service.conf` | new | Apache `VirtualHost *:80`: `WSGIDaemonProcess`, `WSGIScriptAlias /`, `Alias /media/` |
| `web_service.wsgi` | new | mod_wsgi entry point: `application = create_app()` |
| `docker-entrypoint.sh` | new | Waits for PostgreSQL and creates the schema, then `exec apache2ctl -D FOREGROUND` |
| `.env.example` | new | DB name, user, password and host port; copy to `.env` (git-ignored) |
| `.dockerignore` | new | Keeps `.env`, videos, logs and the venv out of the image |
| `web_service.py` | modified | SQLite → PostgreSQL (`psycopg2`), settings from environment, `/health`, `wait-db` command |
| `requirements.txt` | modified | Web service: `flask`, `psycopg2`, `mod-wsgi` (installed into the image) |
| `requirements-recorder.txt` | new (split) | Recorder and client on the host: `playwright`, `requests` |
| `video_client.py`, `capture_video.py`, `qscript.py`, `input.csv` | as is | Recorder and client from Test Tasks 1–2 |

## How it works

### `docker-compose.yml`

- `web` is built from the `Dockerfile` and maps host port `8000` (`WEB_PORT`) to Apache's `80`.
- `db` uses the official `postgres:16` image; its healthcheck is
  `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` every 5 s, 10 retries.
- `web` has `depends_on: db: condition: service_healthy`, so Compose starts it only after the
  database is healthy.
- Named volumes:
  - `db_data` → `/var/lib/postgresql/data` (the database);
  - `uploads` → `/app/uploads` (the video files).
- Both services have `restart: unless-stopped`.

### Configuration: environment only

`web_service.py` reads every connection setting from the environment; nothing is hard-coded.

| Variable | Set by | Meaning |
|---|---|---|
| `DB_HOST`, `DB_PORT` | compose (`db`, `5432`) | Where PostgreSQL is |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `.env` (defaults `videos` / `video_user` / `video_pass`) | The same values are passed to `db` as `POSTGRES_*`, so both containers always agree |
| `WEB_PORT` | `.env` (default `8000`) | Host port |
| `DB_WAIT_SECONDS` | optional (default `60`) | How long `web` waits for the database on startup |
| `UPLOAD_DIR` | `Dockerfile` (`/app/uploads`) | Where the files are stored |

`.env` is in `.gitignore` and `.dockerignore`, so the password is neither committed nor baked
into the image. The compose defaults exist only so that `docker compose up --build` works
on a fresh clone; set `DB_PASSWORD` in `.env` for anything other than local development.

> PostgreSQL applies `POSTGRES_*` only when it initialises an empty `db_data` volume. After
> changing the password in `.env`, either change it in the database too
> (`ALTER USER video_user PASSWORD '...'`) or recreate the volume with `docker compose down -v`.

### Waiting for the database

Two layers, so `web` never crashes on startup and never accepts a request before the
database works:

1. **Compose:** `depends_on ... condition: service_healthy` delays the start of `web` until
   `pg_isready` succeeds.
2. **The container itself:** `docker-entrypoint.sh` runs `python web_service.py wait-db`
   before Apache starts. It retries the connection with back-off (0.5 s → 5 s) for up to
   `DB_WAIT_SECONDS`, then creates the table. This also covers the cases Compose does not:
   `docker compose restart db`, or `web` restarted on its own. If the database never comes
   up, the container exits with a clear error and `restart: unless-stopped` retries.

When the database disappears while the service is running, requests get
`503 {"error": "database unavailable"}` instead of a 500, and the next request after
the database is back works again (every request opens a new connection).

The `web` image also has a Docker `HEALTHCHECK`: `curl -f http://localhost/health`, which
runs `SELECT 1` in PostgreSQL. `docker compose ps` shows `web` as `healthy` only when the
whole chain Apache → mod_wsgi → Flask → PostgreSQL works.

### Apache + mod_wsgi

- **Build.** `mod_wsgi` is installed with `pip install mod-wsgi` in the build stage, so it is
  compiled for exactly the Python in the image (3.12). The system package
  `libapache2-mod-wsgi-py3` would be built for Debian's Python 3.11.
  `mod_wsgi-express module-config` prints the `LoadModule` line, which becomes
  `/etc/apache2/mods-available/wsgi.load`, enabled with `a2enmod wsgi`.
- **Daemon mode.** `WSGIDaemonProcess video-service processes=2 threads=15` runs the app as
  `www-data` in separate processes. `WSGIScriptAlias / ... process-group=...
  application-group=%{GLOBAL}` loads the app when Apache starts, not on the first request.
- **Static files.** `Alias /media/ /app/uploads/` lets Apache serve the videos itself
  (`Range` → `206`, `ETag`, `sendfile`), as the task suggests for production. Unfinished
  uploads (`*.part`) are denied, and directory listing is off. The Flask `/media/` route
  is kept, so `url_for` builds the URLs and the dev server still serves files.
- **Uploads up to 500 MB.** Flask enforces the limit and answers a JSON `413`. Apache's
  `LimitRequestBody` is a hard cap at 600 MB, and `request-timeout` / `socket-timeout` are
  600 s, so large uploads are not cut off.
- **Logs** go to the container's stdout/stderr: `docker compose logs web`.

### SQLite → PostgreSQL

| | Test Task 2 (SQLite) | Now (PostgreSQL) |
|---|---|---|
| Connection | `sqlite3.connect("videos.db")` | `psycopg2.connect(host, port, dbname, user, password)` from `DB_*` |
| Placeholders | `?` | `%s` |
| Primary key | `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| Timestamps | `TEXT DEFAULT (datetime('now'))` | `TIMESTAMPTZ DEFAULT NOW()`; `/videos` returns ISO 8601 |
| Upsert | `ON CONFLICT(company_name) DO UPDATE` | the same, `UNIQUE (company_name)` kept |
| Rows as dicts | `sqlite3.Row` | `RealDictCursor` |
| Connections | one per request | one per request (`flask.g`, closed on teardown): psycopg2 connections are not thread-safe |
| Upload lock | `threading.Lock` (one process) | `pg_advisory_xact_lock` (works across all Apache processes and threads) |
| WAL / busy timeout | needed against "database is locked" | not needed |

The table keeps the same columns as in Test Task 2:

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,   -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,   -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Uploads across several processes.** In Test Task 2 a `threading.Lock` kept two uploads
from choosing the same file name. Under mod_wsgi there are 2 processes, so a thread lock
is no longer enough. Choosing the name, moving the file into place and the upsert now run
in one transaction holding a PostgreSQL advisory lock. The file itself is received into a
`*.part` temp file *before* the lock is taken, so a slow 500 MB upload does not block the
others.

Everything else from Test Task 2 stays: atomic replace with `os.replace`, `_2`, `_3`, ...
for colliding names, `video_<hash>` for non-ASCII names, URLs built from the request's
`Host` (`url_for(..., _external=True)`).

### Data survives restarts

The database lives in the `db_data` volume and the videos in `uploads`.
`docker compose down` removes the containers but not the named volumes, so after
`docker compose up` every record and file is still there. Only `docker compose down -v`
deletes them.

## Acceptance checklist

Checked on macOS (Docker Desktop 28.1, Compose v2.35) from a clean build.

| Item | Verified |
|---|---|
| `docker compose up --build` starts both containers without errors | Yes; `db` and `web` both `healthy` in `docker compose ps` |
| `POST /upload` uploads a video and returns a working URL | `curl` and `UploadVideo(...)`; the URL returns `200 video/webm`, the download is byte-identical, `Range` → `206` |
| `GET /get_video_url?company_name=...` returns the URL | Yes; `GetVideoURL(...)` too; unknown company → `404` |
| After `docker compose down` and `docker compose up`, data and videos are preserved | All rows and files were still there, and the old URLs still served the videos |
| `web` waits for `db` readiness and does not crash on startup | `web` started alone with `db` stopped (`up --no-deps web`) logged "database not ready yet, retrying" and became healthy once `db` started |

Also checked:
- `db` stopped while `web` is running: requests get `503 {"error": "database unavailable"}`,
  and the next request after `db` is back gets `200`, with no restart of `web`;
- 20 parallel uploads for 5 companies: all `200`, one row and one file per company;
- re-upload of the same company keeps one row (`created_at` stays, `updated_at` changes);
- a file-name collision (`ACME GmbH` / `ACME/GmbH` → `ACME_GmbH_2.webm`) and a Cyrillic name
  (`Ромашка` → `video_<hash>.webm`);
- `400` for an empty file, a missing `company_name` and a `.txt` file;
- `/media/../web_service.py` → `404`;
- files in the uploads volume belong to `www-data`, the user the app runs as.

## Web service API

| Method | Path | Request | Response |
|---|---|---|---|
| `POST` | `/upload` | `multipart/form-data`: `file`, `company_name` | `200 {"company_name", "video_url"}` |
| `GET` | `/get_video_url` | `?company_name=<name>` | `200 {"company_name", "video_url"}`, or `404` if unknown |
| `GET` | `/media/<filename>` | — | The video file (`video/webm`, supports `Range` for seeking) |
| `GET` | `/videos` | — | All records as a JSON list (optional endpoint) |
| `GET` | `/health` | — | `200 {"status": "ok"}` if PostgreSQL answers, `503` otherwise |

Example session:

```bash
curl -F "file=@video/Acme Retail Ltd.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=Nobody"
{"error":"company not found","company_name":"Nobody"}          # HTTP 404
```

**Errors.** Every error comes back as JSON `{"error": "..."}`:

| Code | Cause |
|---|---|
| `400` | Missing file, missing or blank `company_name`, empty file, or a file type that is not a video (`.webm`, `.mp4`, `.mkv`, `.mov`, `.ogv`) |
| `404` | Unknown company (JSON), or unknown media file (served by Apache, so an Apache error page) |
| `413` | File larger than 500 MB |
| `503` | PostgreSQL is unavailable |


## Client functions (`video_client.py`)

```python
from video_client import UploadVideo, GetVideoURL

url = UploadVideo("http://127.0.0.1:8000", "video/Acme Retail Ltd.webm", "Acme Retail Ltd")
url = GetVideoURL("http://127.0.0.1:8000", "Acme Retail Ltd")
```

- **`UploadVideo(server_url, video_path, company_name) -> str`** sends a multipart upload
  (`files={"file": (filename, fileobj, mime)}`, `data={"company_name": ...}`) and returns the
  URL.
- **`GetVideoURL(server_url, company_name) -> str`** returns the stored URL. It raises
  `VideoNotFound`, a subclass of `LookupError`, on 404.
- **Errors.** A non-2xx answer or an unreachable server raises `VideoServiceError`, with the
  HTTP status and the server's error message.

## Recorder options (`capture_video.py`, from Test Task 2)

| Option | Default | Meaning |
|---|---|---|
| `csv_file` | `input.csv` | Input CSV |
| `--server URL` | off | Upload every recorded video to this web service and preview it |
| `--view-seconds N` | `10` | Preview time per video. `0` keeps the preview open until Enter is pressed |
| `--out` | `./video` | Video folder (overrides `qscript.PATH_TO_VIDEO_FOLDER`) |
| `--retries` | `1` | Extra recording attempts for a failed row |
| `--headless` | off | Hide the recording browser (DuckDuckGo blocks headless, see Notes) |
| `--report` | `logs/report.csv` | Per row: recording status, video, attempts, clicked result, `upload`, `video_url`, `upload_error` |
| `--log-file` | `logs/capture.log` | Debug log with tracebacks |

**How the preview behaves**
- It opens the returned URL with Playwright (`headless=False`) and waits until the `<video>`
  element is actually playing.
- `--view-seconds N`: closes after N seconds, or earlier if you close the window.
- `--view-seconds 0`: waits for Enter. Without a terminal (for example in CI) it closes
  right away instead of hanging.

**Failures**
- A failed upload or preview is logged and recorded in the report. The batch continues with
  the next row.

Exit codes:
- `0`: everything ok;
- `1`: a recording or an upload failed;
- `2`: bad input, or `--server` was given but `requests` is not installed.


## Test Task 1: how the recorder works

For each CSV row (`URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`),
`capture_video.py` calls `qscript.CaptureVideo_Magento(...)`. The function records the
scenario with `record_video_dir=PATH_TO_VIDEO_FOLDER` and `record_video_size=1280×720`.
It closes the context to finalize the file, saves it as
`<PATH_TO_VIDEO_FOLDER>/<company_name>.webm` and returns the absolute path, or `None`.

### Overlay
- **Injection.** The SVG cursor (white arrow, black outline), the yellow highlight, the
  click ripple and the callout are injected with `context.add_init_script`, so they survive
  the full-page navigation of the search.
- **CSP safety.** Styles go through the CSSOM and animations through the Web Animations API,
  with no `<style>` and no `innerHTML`. Strict-CSP pages still render the overlay.
- **No interference.** The overlay has `pointer-events: none` and is topmost.

### Mouse and keyboard
- The mouse moves along an eased, curved path in small steps.
- Clicks are real mouse down/up events, so form submit and link navigation happen as normal.
- The query is typed one character at a time, with 60–180 ms delays.

### Results and the first result
- **Visible selectors only.** DuckDuckGo has a hidden duplicate submit button.
- **Blank results page.** Pages that are still blank after 15 s are reloaded once.
- **Reliable click.**
  - The script waits for the layout to settle and re-aims if the target moved.
  - `elementFromPoint` confirms that the first result is under the cursor; a popup that
    covers it is hidden.
  - `target="_blank"` is removed, so the landing page opens in the recorded tab.

### Failures and file names
- **Failures.**
  - Every attempt has a 120 s budget, and a failed row is retried.
  - On final failure the last partial video is kept as `<name>_FAILED.webm`.
- **File names.**
  - Characters that are not allowed become `_`.
  - Reserved Windows names get a `_` prefix.
  - Duplicate names within one run get `_2`, `_3`, ...


## Notes

- **Image.** `python:3.12-slim-bookworm` + Debian's Apache 2.4; the `web` image does not
  contain compilers (they stay in the build stage).
- **Local run without Docker** still works for development: start PostgreSQL, export
  `DB_HOST`, `DB_PASSWORD`, ... and run `python web_service.py` (Flask dev server on `:8000`,
  which then also serves `/media/` itself).
- **Recorder on the host.** `capture_video.py` is unchanged and runs on the host, not in a
  container: it needs a headed Chromium (DuckDuckGo blocks headless, see Test Task 1).
- **DuckDuckGo** is a live site: the order of results changes between runs, and its
  selectors are constants at the top of `qscript.py`.
