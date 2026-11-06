"""Client functions for web_service.py, used by the recorder."""

import mimetypes
import os

import requests

# (connect, read) seconds; the read timeout covers uploading a large file.
TIMEOUT = (5, 300)

mimetypes.add_type("video/webm", ".webm")


class VideoServiceError(RuntimeError):
    """The web service answered with an error or could not be reached."""


class VideoNotFound(VideoServiceError, LookupError):
    """GetVideoURL: the service has no video for this company (HTTP 404)."""


def _json(resp):
    """Parsed JSON of a successful response; VideoServiceError otherwise."""
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not resp.ok:
        detail = body.get("error") if isinstance(body, dict) else None
        cls = VideoNotFound if resp.status_code == 404 else VideoServiceError
        raise cls(f"{resp.request.method} {resp.url} -> HTTP {resp.status_code}: "
                  f"{detail or resp.text[:200]}")
    if not isinstance(body, dict) or "video_url" not in body:
        raise VideoServiceError(f"{resp.url}: unexpected response {resp.text[:200]!r}")
    return body


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    mime = mimetypes.guess_type(video_path)[0] or "application/octet-stream"
    try:
        with open(video_path, "rb") as f:
            resp = requests.post(
                server_url.rstrip("/") + "/upload",
                files={"file": (os.path.basename(video_path), f, mime)},
                data={"company_name": company_name},
                timeout=TIMEOUT,
            )
    except requests.RequestException as e:
        raise VideoServiceError(f"upload to {server_url} failed: {e}") from e
    return _json(resp)["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name.

    Raises VideoNotFound if the service has no video for this company.
    """
    try:
        resp = requests.get(server_url.rstrip("/") + "/get_video_url",
                            params={"company_name": company_name}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise VideoServiceError(f"request to {server_url} failed: {e}") from e
    return _json(resp)["video_url"]
