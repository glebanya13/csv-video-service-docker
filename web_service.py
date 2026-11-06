"""Web service for recorded videos: upload, look up by company name, serve.

In Docker it runs under Apache + mod_wsgi (see web_service.wsgi, apache/video-service.conf).
Locally, for development:  python web_service.py  (Flask dev server on 0.0.0.0:8000).

Database: PostgreSQL. Connection settings come only from the environment:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
    DB_WAIT_SECONDS  how long to wait for the database on startup (default 60)
    UPLOAD_DIR       where video files are stored (default ./uploads)

Endpoints:
    POST /upload                         multipart: file, company_name -> {company_name, video_url}
    GET  /get_video_url?company_name=... -> {company_name, video_url}, 404 if unknown
    GET  /media/<filename>               the stored video file (supports HTTP Range)
    GET  /videos                         all records
    GET  /health                         200 {"status": "ok"} if the database answers, else 503

CLI:
    python web_service.py wait-db        wait for PostgreSQL and create the schema, then exit
"""

import hashlib
import logging
import mimetypes
import os
import sys
import tempfile
import time

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, request, send_from_directory, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or os.path.join(BASE_DIR, "uploads")
MAX_UPLOAD_MB = 500
ALLOWED_EXTENSIONS = {".webm", ".mp4", ".mkv", ".mov", ".ogv"}

# Key for pg_advisory_xact_lock: serialises "pick a file name + write the row" across all
# Apache processes and threads (a threading.Lock would only cover one process).
UPLOAD_LOCK_KEY = 0x7669646571  # "videq"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,   -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,   -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

UPSERT = """
INSERT INTO videos (company_name, file_path, video_url) VALUES (%s, %s, %s)
ON CONFLICT (company_name) DO UPDATE SET
    file_path  = EXCLUDED.file_path,
    video_url  = EXCLUDED.video_url,
    updated_at = NOW()
"""

# Not every OS registers .webm; without it the file would be served as octet-stream.
mimetypes.add_type("video/webm", ".webm")

log = logging.getLogger("web_service")


def db_settings():
    """psycopg2.connect() keyword arguments, read from the environment (nothing hard-coded)."""
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ.get("DB_NAME", "videos"),
        "user": os.environ.get("DB_USER", "video_user"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "connect_timeout": 5,
        "application_name": "video-web-service",
    }


def _connect(settings):
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **settings)


def init_db(settings, wait_seconds=None):
    """Wait until PostgreSQL accepts connections, then create the schema (idempotent)."""
    if wait_seconds is None:
        wait_seconds = float(os.environ.get("DB_WAIT_SECONDS", "60"))
    deadline = time.monotonic() + wait_seconds
    delay = 0.5
    while True:
        try:
            conn = _connect(settings)
            break
        except psycopg2.OperationalError as e:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"PostgreSQL at {settings['host']}:{settings['port']} "
                                   f"is not reachable after {wait_seconds:.0f}s: {e}") from e
            log.warning("database not ready yet (%s), retrying in %.1fs",
                        str(e).strip().splitlines()[0], delay)
            time.sleep(delay)
            delay = min(delay * 2, 5)
    try:
        with conn, conn.cursor() as cur:
            # Two processes starting at once must not race on CREATE TABLE.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (UPLOAD_LOCK_KEY + 1,))
            cur.execute(SCHEMA)
    finally:
        conn.close()
    log.info("database ready: %s@%s:%s/%s", settings["user"], settings["host"],
             settings["port"], settings["dbname"])


def _file_stem(company_name):
    """ASCII file name for a company ("ACME GmbH" -> "ACME_GmbH")."""
    stem = secure_filename(company_name)[:100]
    if not stem:
        # secure_filename drops non-ASCII, so e.g. a Cyrillic-only name becomes empty.
        stem = "video_" + hashlib.sha1(company_name.encode("utf-8")).hexdigest()[:10]
    return stem


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def create_app(db=None, upload_dir=UPLOAD_DIR):
    app = Flask(__name__)
    app.config.update(DB=db or db_settings(), UPLOAD_DIR=upload_dir,
                      MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024)
    app.json.sort_keys = False  # keep {"company_name", "video_url"} in the documented order
    app.json.ensure_ascii = False  # "Ромашка", not "Ро..."
    os.makedirs(upload_dir, exist_ok=True)
    init_db(app.config["DB"])

    def db_conn():
        """A new connection per request: psycopg2 connections must not be shared between
        threads, and mod_wsgi serves requests from several threads and processes."""
        if "db" not in g:
            g.db = _connect(app.config["DB"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(HTTPException)
    def json_error(e):
        return jsonify(error=e.description), e.code

    @app.errorhandler(psycopg2.OperationalError)
    def db_unavailable(e):
        log.error("database error: %s", e)
        return jsonify(error="database unavailable"), 503

    def bad_request(message):
        return jsonify(error=message), 400

    def pick_filename(cur, company_name, ext):
        """File name for this company; never one that belongs to another company."""
        stem = _file_stem(company_name)
        name, n = stem + ext, 2
        while True:
            path = os.path.join(app.config["UPLOAD_DIR"], name)
            cur.execute("SELECT company_name FROM videos WHERE file_path = %s", (path,))
            owner = cur.fetchone()
            if owner is None or owner["company_name"] == company_name:
                return name
            name, n = f"{stem}_{n}{ext}", n + 1

    @app.post("/upload")
    def upload():
        file = request.files.get("file")
        company_name = (request.form.get("company_name") or "").strip()
        if file is None or not file.filename:
            return bad_request("missing 'file' (multipart/form-data)")
        if not company_name:
            return bad_request("missing 'company_name'")
        ext = os.path.splitext(file.filename)[1].lower() or ".webm"
        if ext not in ALLOWED_EXTENSIONS:
            return bad_request(f"unsupported file type {ext!r}; allowed: "
                               + ", ".join(sorted(ALLOWED_EXTENSIONS)))

        upload_dir = app.config["UPLOAD_DIR"]
        # Write to a temp file first (outside the lock, uploads can be large), then swap it
        # in: a re-upload never leaves a half-written video behind the URL.
        fd, tmp = tempfile.mkstemp(dir=upload_dir, suffix=".part")
        os.close(fd)
        old_path = None
        try:
            file.save(tmp)
            if os.path.getsize(tmp) == 0:
                return bad_request("uploaded file is empty")
            os.chmod(tmp, 0o644)  # mkstemp creates 0600; Apache serves /media/ itself

            conn = db_conn()
            with conn, conn.cursor() as cur:  # one transaction; commit on success
                # Choosing a file name and writing the record must not interleave between
                # requests, or two companies with the same sanitised name could share a file.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (UPLOAD_LOCK_KEY,))
                name = pick_filename(cur, company_name, ext)
                path = os.path.join(upload_dir, name)
                cur.execute("SELECT file_path FROM videos WHERE company_name = %s",
                            (company_name,))
                old = cur.fetchone()
                video_url = url_for("media", filename=name, _external=True)  # from Host
                os.replace(tmp, path)
                cur.execute(UPSERT, (company_name, path, video_url))
            if old and old["file_path"] != path:
                old_path = old["file_path"]
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        # Re-upload with another extension: drop the previous file of this company.
        if old_path and os.path.exists(old_path):
            os.remove(old_path)
        log.info("stored %r -> %s", company_name, path)
        return jsonify(company_name=company_name, video_url=video_url)

    @app.get("/get_video_url")
    def get_video_url():
        company_name = (request.args.get("company_name") or "").strip()
        if not company_name:
            return bad_request("missing 'company_name' query parameter")
        with db_conn().cursor() as cur:
            cur.execute("SELECT company_name, video_url FROM videos WHERE company_name = %s",
                        (company_name,))
            row = cur.fetchone()
        if row is None:
            return jsonify(error="company not found", company_name=company_name), 404
        return jsonify(company_name=row["company_name"], video_url=row["video_url"])

    @app.get("/media/<path:filename>")
    def media(filename):
        # In Docker, Apache serves /media/ straight from the uploads volume (Alias) and this
        # route is only used to build URLs. With the dev server it serves the files:
        # send_from_directory rejects "../" escapes and answers Range requests (seeking).
        return send_from_directory(app.config["UPLOAD_DIR"], filename)

    @app.get("/videos")
    def videos():
        with db_conn().cursor() as cur:
            cur.execute("SELECT id, company_name, file_path, video_url, created_at,"
                        " updated_at FROM videos ORDER BY id")
            rows = cur.fetchall()
        return jsonify([{k: _iso(v) for k, v in r.items()} for r in rows])

    @app.get("/health")
    def health():
        with db_conn().cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify(status="ok")

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["wait-db"]:
        try:
            init_db(db_settings())
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(1)
        sys.exit(0)
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    create_app().run(host=host, port=port, threaded=True)
