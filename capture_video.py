"""Read input CSV and record one video per row via qscript.CaptureVideo_Magento.

With --server, every recorded video is also uploaded to web_service.py and
previewed in Chromium.
"""

import argparse
import collections
import csv
import logging
import os
import sys

import qscript

REQUIRED_COLUMNS = ["URL", "Company Name", "Language", "Country",
                    "Postcode", "City", "Street", "HouseNo"]
REPORT_COLUMNS = ["row", "company", "status", "video", "attempts", "seconds",
                  "clicked", "final_url", "error", "upload", "video_url", "upload_error"]
PLAYING_JS = "() => { const v = document.querySelector('video'); return !!v && v.readyState >= 2; }"

log = logging.getLogger("capture_video")


class CsvFormatError(Exception):
    pass


def read_rows(path):
    """Return the CSV rows as dicts with stripped keys and values.

    Raises CsvFormatError if required columns are missing.
    """
    # utf-8-sig: files saved by Excel start with a BOM that would end up in "URL".
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise CsvFormatError(
                f"{path}: missing column(s) {', '.join(missing)}; "
                f"found {header or 'no header'}; expected {';'.join(REQUIRED_COLUMNS)}"
            )
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k}
                for row in reader]


def setup_logging(log_file):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    handlers = [console]
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def write_report(path, records):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS, delimiter=";",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def preview(video_url, view_seconds):
    """Open the uploaded video in Chromium, which shows its built-in media player.

    view_seconds > 0: keep it open that long (or until the window is closed);
    0: keep it open until Enter is pressed.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        try:
            page = browser.new_page(viewport=qscript.VIDEO_SIZE)
            resp = page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
            if resp is None or not resp.ok:
                raise RuntimeError(f"HTTP {resp.status if resp else '?'} for {video_url}")
            try:
                page.wait_for_function(PLAYING_JS, timeout=15000)
                log.info("    preview: video is playing in Chromium")
            except PlaywrightTimeoutError:
                log.warning("    preview: the video did not start playing within 15 s")
            if view_seconds > 0:
                try:
                    page.wait_for_event("close", timeout=view_seconds * 1000)
                except PlaywrightTimeoutError:
                    pass  # the normal case: time is up
            else:
                try:
                    input("    preview is open, press Enter to continue... ")
                except EOFError:
                    log.warning("    preview: no terminal to read Enter from, closing")
        finally:
            browser.close()


def upload_and_preview(client, server, video_path, company_name, view_seconds):
    """Upload one video and preview it. Returns report fields; never raises."""
    try:
        video_url = client.UploadVideo(server, video_path, company_name)
    except Exception as e:  # noqa: BLE001 - a failed upload must not stop the batch
        log.error("    upload FAILED: %s", e)
        return {"upload": "failed", "upload_error": str(e)}
    log.info("    uploaded: %s", video_url)
    try:
        preview(video_url, view_seconds)
    except Exception as e:  # noqa: BLE001 - neither must a failed preview
        log.error("    preview FAILED: %s", e)
        return {"upload": "ok", "video_url": video_url, "upload_error": f"preview: {e}"}
    return {"upload": "ok", "video_url": video_url}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_file", nargs="?", default="input.csv")
    parser.add_argument("--out", default=qscript.PATH_TO_VIDEO_FOLDER,
                        help="video folder (default: %(default)s)")
    parser.add_argument("--headless", action="store_true",
                        help="hide the browser window (DuckDuckGo may block it)")
    parser.add_argument("--retries", type=int, default=qscript.RETRIES,
                        help="extra attempts per failed row (default: %(default)s)")
    # Kept out of the video folder, so it holds only the .webm files.
    parser.add_argument("--report", default="logs/report.csv",
                        help="CSV report path (default: %(default)s)")
    parser.add_argument("--log-file", default="logs/capture.log",
                        help="debug log path (default: %(default)s)")
    parser.add_argument("--server", metavar="URL",
                        help="web_service.py base URL, e.g. http://127.0.0.1:8000: "
                             "upload and preview every recorded video")
    parser.add_argument("--view-seconds", type=float, default=10, metavar="N",
                        help="preview time per video; 0 = until Enter (default: %(default)s)")
    args = parser.parse_args(argv)
    if args.view_seconds < 0:
        parser.error("--view-seconds must be >= 0")
    if args.server and not args.server.startswith(("http://", "https://")):
        parser.error("--server must be an http(s) URL, e.g. http://127.0.0.1:8000")

    qscript.PATH_TO_VIDEO_FOLDER = args.out
    qscript.HEADLESS = args.headless
    qscript.RETRIES = max(0, args.retries)
    report_path = args.report
    setup_logging(args.log_file)

    try:
        rows = read_rows(args.csv_file)
    except (OSError, UnicodeDecodeError, CsvFormatError, csv.Error) as e:
        log.error("cannot read input: %s", e)
        return 2

    client = None
    if args.server:
        # Imported only here, so the recorder runs without `requests` when --server is unused.
        try:
            import video_client as client
        except ImportError as e:
            log.error("--server needs the requests package (pip install -r requirements.txt): %s", e)
            return 2

    dupes = [n for n, c in collections.Counter(r["Company Name"] for r in rows).items() if c > 1]
    if dupes:
        log.warning("duplicate company names (videos get a _2, _3 suffix): %s", ", ".join(dupes))

    records = []
    for n, row in enumerate(rows, 1):
        record = {"row": n, "company": row["Company Name"]}
        if not row["URL"]:
            log.warning("[%d] %s: empty URL, skipped", n, row["Company Name"])
            records.append(dict(record, status="skipped", error="empty URL"))
            continue
        log.info("[%d/%d] %s (%s) -> %s", n, len(rows), row["Company Name"],
                 row["City"], row["URL"])
        path = qscript.CaptureVideo_Magento(
            row["URL"],
            row["Company Name"],
            row["Language"],
            row["Country"],
            row["Postcode"],
            row["City"],
            row["Street"],
            row["HouseNo"],
        )
        record.update(qscript.LAST_RUN)
        if path:
            print(f"    saved: {path}", flush=True)
            if client:
                record.update(upload_and_preview(client, args.server, path, row["Company Name"],
                                                 args.view_seconds))
        else:
            log.error("    FAILED: %s", qscript.LAST_RUN.get("error"))
        records.append(record)

    write_report(report_path, records)
    ok = sum(r.get("status") == "ok" for r in records)
    failed = sum(r.get("status") == "failed" for r in records)
    skipped = len(records) - ok - failed
    summary = f"Done: {ok} ok, {failed} failed, {skipped} skipped"
    upload_failed = sum(r.get("upload") == "failed" for r in records)
    if client:
        uploaded = sum(r.get("upload") == "ok" for r in records)
        summary += f"; uploaded {uploaded}, upload failed {upload_failed}"
    log.info("%s. Report: %s", summary, os.path.abspath(report_path))
    return 1 if failed or upload_failed else 0


if __name__ == "__main__":
    sys.exit(main())
