"""Download engine: wraps yt-dlp and runs it on a background thread.

Security notes
--------------
* User input is never passed to yt-dlp as-is. Only a validated video ID is
  kept, percent-encoded, and joined to fixed constants (ensure_safe_url).
* Nothing from an exception, the URL or yt-dlp's output is ever shown or
  logged. Errors are put into categories and turned into fixed, pre-written
  messages (see _ERROR_CATEGORIES), so credentials, cookies, signed URLs or
  local paths cannot leak through error text.
* yt-dlp gets a logger that throws everything away, so it writes nothing to
  stdout/stderr or to any log file.
"""

import os
import re
import shutil
import threading
from urllib.parse import quote, urlencode, urlunsplit

import yt_dlp

from url_utils import InvalidURLError, extract_video_id

HAS_FFMPEG = shutil.which("ffmpeg") is not None

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
# The only URL shape ever handed to yt-dlp.
_CANONICAL_URL_RE = re.compile(r"https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}")

# label -> (format when ffmpeg is available, format without ffmpeg)
QUALITY_FORMATS = {
    "Best available": ("bv*+ba/b", "b"),
    "1080p": ("bv*[height<=1080]+ba/b[height<=1080]", "b[height<=1080]/b"),
    "720p": ("bv*[height<=720]+ba/b[height<=720]", "b[height<=720]/b"),
    "480p": ("bv*[height<=480]+ba/b[height<=480]", "b[height<=480]/b"),
    "Audio only": ("ba/b", "ba/b"),
}


class DownloadCancelled(Exception):
    """Raised from the progress hook when the user presses Cancel."""


def ensure_safe_url(raw_url: str) -> str:
    """Validate *raw_url* and return a freshly built, encoded watch URL."""
    video_id = extract_video_id(raw_url)          # parse + allow-list checks
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise InvalidURLError("Invalid video ID.")

    encoded_id = quote(video_id, safe="")         # explicit context encoding
    if encoded_id != video_id:                    # a valid ID never changes
        raise InvalidURLError("Video ID contains unexpected characters.")

    query = urlencode({"v": encoded_id}, safe="", quote_via=quote)
    safe_url = urlunsplit(("https", "www.youtube.com", "/watch", query, ""))

    if not _CANONICAL_URL_RE.fullmatch(safe_url):
        raise InvalidURLError("Link failed canonical-form check.")
    return safe_url


# --- Error handling: fixed messages only ------------------------------------
# The exception text is read *only* to pick a category. It is never copied
# into the message that gets returned.

_ERROR_CATEGORIES = (
    (("private video",), "This video is private."),
    (("sign in to confirm your age", "age-restricted", "age restricted"),
     "This video is age-restricted and cannot be downloaded without signing in."),
    (("video unavailable", "has been removed", "no longer available"),
     "This video is unavailable or has been removed."),
    (("not available in your country", "geo", "blocked it in your country"),
     "This video is not available in your region."),
    (("members-only", "join this channel"), "This video is for channel members only."),
    (("premiere", "live event will begin", "is upcoming"),
     "This video has not been released yet."),
    (("requested format is not available", "format is not available"),
     "The selected quality is not available for this video. Try another option."),
    (("ffmpeg", "ffprobe"),
     "Post-processing failed. Make sure ffmpeg is installed correctly."),
    (("http error 429", "too many requests"),
     "YouTube is rate-limiting requests. Please wait and try again later."),
    (("http error 403", "forbidden"),
     "YouTube refused the request. Updating yt-dlp usually fixes this."),
    (("timed out", "timeout", "connection", "network", "name resolution",
      "getaddrinfo", "urlopen error"),
     "Network error. Check your internet connection and try again."),
    (("no space left", "disk full"), "Not enough disk space in the download folder."),
    (("permission denied", "access is denied", "read-only file system"),
     "Cannot write to the download folder (permission denied)."),
)
_GENERIC_DOWNLOAD_ERROR = "The download failed. Please try again or pick a different quality."
_GENERIC_UNEXPECTED_ERROR = "Something unexpected went wrong. Please try again."


def describe_error(exc: BaseException) -> str:
    """Pick a fixed, user-friendly message for *exc*. No exception data is
    copied into the result."""
    if isinstance(exc, OSError) and not isinstance(exc, (ConnectionError, TimeoutError)):
        if getattr(exc, "errno", None) == 28:          # ENOSPC
            return "Not enough disk space in the download folder."
        if isinstance(exc, PermissionError):
            return "Cannot write to the download folder (permission denied)."
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return "Network error. Check your internet connection and try again."

    probe = str(exc).lower()
    for needles, message in _ERROR_CATEGORIES:
        if any(n in probe for n in needles):
            return message
    if isinstance(exc, yt_dlp.utils.DownloadError):
        return _GENERIC_DOWNLOAD_ERROR
    return _GENERIC_UNEXPECTED_ERROR


class _SilentLogger:
    """yt-dlp logger that throws away all output, so nothing is ever logged."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


_TITLE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_TITLE_LEN = 120


def _display_title(title) -> str:
    """Plain-text title for a Tk label: no control characters, length capped."""
    text = _TITLE_CTRL_RE.sub(" ", str(title or "video")).strip() or "video"
    if len(text) > _MAX_TITLE_LEN:
        text = text[:_MAX_TITLE_LEN].rstrip() + "…"
    return text


# --- Downloader ---------------------------------------------------------------

class Downloader:
    """Runs one download at a time and reports progress through callbacks.

    Callbacks are invoked from the worker thread; the UI must marshal them
    onto its own thread (see app.py).
    """

    def __init__(self, on_progress, on_status, on_done):
        self.on_progress = on_progress   # (percent: float, text: str)
        self.on_status = on_status       # (text: str)
        self.on_done = on_done           # (ok: bool, message: str)
        self._cancel = threading.Event()
        self._thread = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, raw_url: str, out_dir: str, quality: str) -> None:
        if self.busy:
            raise RuntimeError("A download is already running.")

        # Validate and encode BEFORE anything else touches the input.
        safe_url = ensure_safe_url(raw_url)

        out_dir = os.path.realpath(out_dir)
        if not os.path.isdir(out_dir):
            raise ValueError("The download folder does not exist.")
        if quality not in QUALITY_FORMATS:
            raise ValueError("Unknown quality option.")

        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(safe_url, out_dir, quality), daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _build_opts(self, out_dir: str, quality: str) -> dict:
        with_ff, without_ff = QUALITY_FORMATS[quality]
        opts = {
            "format": with_ff if HAS_FFMPEG else without_ff,
            "outtmpl": os.path.join(out_dir, "%(title).150B [%(id)s].%(ext)s"),
            "paths": {"home": out_dir},
            "restrictfilenames": True,   # ASCII-only, no spaces/shell chars
            "windowsfilenames": True,
            "noplaylist": True,          # only ever the single validated video
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp_hook],
            "logger": _SilentLogger(),   # nothing is logged anywhere
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        if HAS_FFMPEG:
            if quality == "Audio only":
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }]
            else:
                opts["merge_output_format"] = "mp4"
        return opts

    def _run(self, safe_url: str, out_dir: str, quality: str) -> None:
        try:
            # Re-check right at the point of use.
            safe_url = ensure_safe_url(safe_url)
            self.on_status("Fetching video info…")
            with yt_dlp.YoutubeDL(self._build_opts(out_dir, quality)) as ydl:
                info = ydl.extract_info(safe_url, download=True)
            self.on_done(True, f"Finished: {_display_title((info or {}).get('title'))}")
        except DownloadCancelled:
            self.on_done(False, "Download cancelled.")
        except InvalidURLError as exc:
            # Our own fixed messages from url_utils; no user data echoed back.
            self.on_done(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - map everything to a fixed message
            self.on_done(False, describe_error(exc))

    def _hook(self, d: dict) -> None:
        if self._cancel.is_set():
            raise DownloadCancelled()

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total * 100) if total else 0.0
            speed = d.get("speed") or 0
            eta = d.get("eta")
            text = f"{_fmt_bytes(done)} / {_fmt_bytes(total)}"
            if speed:
                text += f"  •  {_fmt_bytes(speed)}/s"
            if eta is not None:
                text += f"  •  ETA {int(eta)}s"
            self.on_progress(pct, text)
        elif status == "finished":
            self.on_progress(100.0, "Download complete, processing…")

    def _pp_hook(self, d: dict) -> None:
        if d.get("status") == "started":
            self.on_status("Post-processing…")


def _fmt_bytes(n: float) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
