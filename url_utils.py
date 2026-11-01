"""Validation and canonicalisation of user-supplied YouTube URLs.

User input is never handed to the downloader as-is. Instead we:
  1. reject anything with whitespace / control characters or that is too long,
  2. parse it with urllib.parse and enforce scheme, host allow-list and port,
  3. extract the 11-character video ID and check it against a strict regex,
  4. rebuild a brand-new URL from trusted constants plus the encoded video ID.
"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

ALLOWED_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({None, 80, 443})
MAX_URL_LENGTH = 2048

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")


class InvalidURLError(ValueError):
    """Raised when the input is not an acceptable YouTube video link."""


def extract_video_id(raw: str) -> str:
    """Return the validated video ID contained in *raw*, or raise InvalidURLError."""
    if not isinstance(raw, str):
        raise InvalidURLError("The link must be text.")

    url = raw.strip()
    if not url:
        raise InvalidURLError("Please paste a YouTube link.")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURLError("That link is too long.")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise InvalidURLError("The link contains spaces or control characters.")

    if "://" not in url:
        url = "https://" + url

    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise InvalidURLError("Only http(s) links are supported.")
    if parts.username or parts.password:
        raise InvalidURLError("Links with embedded credentials are not allowed.")

    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise InvalidURLError("That is not a YouTube link.")

    try:
        port = parts.port
    except ValueError:
        raise InvalidURLError("The link has an invalid port.") from None
    if port not in ALLOWED_PORTS:
        raise InvalidURLError("The link has an unexpected port.")

    candidate = None
    path = parts.path
    if host == "youtu.be":
        candidate = path.lstrip("/").split("/", 1)[0]
    elif path.rstrip("/") == "/watch":
        candidate = parse_qs(parts.query).get("v", [None])[0]
    else:
        for prefix in _PATH_PREFIXES:
            if path.startswith(prefix):
                candidate = path[len(prefix):].split("/", 1)[0]
                break

    if not candidate or not _VIDEO_ID_RE.fullmatch(candidate):
        raise InvalidURLError("Could not find a valid video ID in that link.")
    return candidate


def canonical_watch_url(video_id: str) -> str:
    """Build a canonical, properly encoded watch URL from a validated video ID."""
    if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
        raise InvalidURLError("Invalid video ID.")
    query = urlencode({"v": video_id})
    return urlunsplit(("https", "www.youtube.com", "/watch", query, ""))


def validate_youtube_url(raw: str) -> str:
    """Validate *raw* and return a safe, canonical YouTube watch URL."""
    return canonical_watch_url(extract_video_id(raw))
