"""YouTube upload (Data API v3) and read access for YouTube Analytics API v2.

One OAuth token is reused for both APIs, so the requested scopes already cover
the Analytics read used to learn from average view percentage.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import List, Optional, Sequence

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

TOKEN_PATH = "token.json"
CLIENT_SECRET_PATH = "client_secret.json"
# A CI run has no token file, so the secret carries the same JSON as the text
# of an environment variable (the Drive side does the same with its token).
TOKEN_JSON_ENV = "YOUTUBE_TOKEN_JSON"

DEFAULT_CATEGORY_ID = "22"
DEFAULT_LANGUAGE = "en"
TITLE_MAX_LENGTH = 100
DESCRIPTION_MAX_LENGTH = 5000
TAG_MAX_LENGTH = 500
ANALYTICS_METRIC = "averageViewPercentage"


def _normalize_scope(scope: str) -> str:
    return scope.rstrip("/")


def scopes_cover(granted: Sequence[str], required: Sequence[str] = SCOPES) -> bool:
    """True when the stored token already carries every scope we need."""
    granted_set = {_normalize_scope(scope) for scope in (granted or [])}
    return all(_normalize_scope(scope) in granted_set for scope in required)


def credentials_from_env(env: Optional[dict] = None):
    """Credentials from ``YOUTUBE_TOKEN_JSON``, or ``None`` when it is not set.

    The value is the content of ``token.json`` (an authorized-user JSON with a
    refresh token), which GitHub Actions passes from a repository secret so a
    scheduled run needs no token file on the runner. An expired token is still
    refreshed from that refresh token.
    """
    source = os.environ if env is None else env
    token_json = str(source.get(TOKEN_JSON_ENV, "") or "").strip()
    if not token_json:
        return None

    from google.oauth2.credentials import Credentials

    try:
        info = json.loads(token_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TOKEN_JSON_ENV} is not valid JSON") from exc
    if not isinstance(info, dict) or not info.get("refresh_token"):
        raise ValueError(
            f"{TOKEN_JSON_ENV} must hold an authorized-user JSON that includes a "
            "refresh_token (the content of token.json)."
        )
    return Credentials.from_authorized_user_info(info, SCOPES)


def load_credentials(force_authorize: bool = False):
    """Load cached OAuth credentials, refreshing or re-authorizing as needed.

    ``YOUTUBE_TOKEN_JSON`` wins over ``token.json`` when both are present: in a
    CI run it is the only source. The Analytics scope was added later, so a
    token issued before that gets re-authorized instead of failing halfway
    through a run.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if not force_authorize:
        creds = credentials_from_env()
        if creds is not None and not scopes_cover(getattr(creds, "scopes", []) or []):
            creds = None
    if creds is None and os.path.exists(TOKEN_PATH) and not force_authorize:
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not scopes_cover(getattr(creds, "scopes", []) or []):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_PATH):
                raise FileNotFoundError(
                    f"{CLIENT_SECRET_PATH} not found. Download the OAuth client "
                    "(Desktop app) JSON from Google Cloud Console and place it "
                    "in the project root, or set "
                    f"{TOKEN_JSON_ENV} to the content of token.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
    return creds


def get_youtube_service(credentials=None):
    from googleapiclient.discovery import build

    creds = credentials or load_credentials()
    return build("youtube", "v3", credentials=creds)


def parse_published_at(value: object) -> Optional[datetime]:
    """A YouTube RFC 3339 timestamp as an aware UTC datetime."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unreadable YouTube timestamp: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def get_latest_upload_time(youtube=None) -> Optional[datetime]:
    """When the newest video on the channel was published (None: none yet).

    Two read calls: ``channels.list(part="contentDetails", mine=True)`` gives
    the uploads playlist, and ``playlistItems.list(maxResults=1)`` on it gives
    the newest entry, which YouTube keeps first. That publish time is the only
    honest basis for the posting interval: a local file time or a leftover
    ``02_READY_TO_POST`` package from an earlier test says nothing about what
    the channel actually shows.
    """
    if youtube is None:
        youtube = get_youtube_service()

    channel = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = (channel or {}).get("items") or []
    if not items:
        return None
    details = items[0].get("contentDetails") or {}
    uploads = str((details.get("relatedPlaylists") or {}).get("uploads") or "")
    if not uploads:
        return None

    listing = (
        youtube.playlistItems()
        .list(part="snippet", playlistId=uploads, maxResults=1)
        .execute()
    )
    entries = (listing or {}).get("items") or []
    if not entries:
        return None
    snippet = (entries[0] or {}).get("snippet") or {}
    published = parse_published_at(snippet.get("publishedAt"))
    if published is None:
        raise ValueError("the newest upload has no publishedAt")
    return published


def get_youtube_analytics_service(credentials=None):
    from googleapiclient.discovery import build

    creds = credentials or load_credentials()
    return build("youtubeAnalytics", "v2", credentials=creds)


def build_upload_body(
    metadata: dict,
    privacy_status: str = "unlisted",
    category_id: str = DEFAULT_CATEGORY_ID,
    default_language: str = DEFAULT_LANGUAGE,
) -> dict:
    """Assemble the videos.insert body, trimmed to YouTube's field limits.

    The ``privacy_status`` default stays ``unlisted`` on purpose: config.yaml
    decides the real privacy (``public`` there), so a call that forgets to pass
    it cannot go live by accident.
    """
    tags: List[str] = []
    total = 0
    for tag in metadata.get("tags", []) or []:
        tag = str(tag).strip()
        if not tag:
            continue
        if total + len(tag) + 1 > TAG_MAX_LENGTH:
            break
        tags.append(tag)
        total += len(tag) + 1

    return {
        "snippet": {
            "title": str(metadata.get("title", "")).strip()[:TITLE_MAX_LENGTH],
            "description": str(metadata.get("description", ""))[:DESCRIPTION_MAX_LENGTH],
            "tags": tags,
            "categoryId": str(category_id),
            "defaultLanguage": str(default_language),
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }


def build_analytics_query(video_id: str, start_date: str, end_date: str) -> dict:
    """Parameters for youtubeAnalytics.reports().query for one video."""
    return {
        "ids": "channel==MINE",
        "startDate": start_date,
        "endDate": end_date,
        "metrics": ANALYTICS_METRIC,
        "filters": f"video=={video_id}",
    }


def upload_short(
    video_path: str,
    metadata: dict,
    privacy_status: str = "unlisted",
    youtube=None,
    category_id: str = DEFAULT_CATEGORY_ID,
    default_language: str = DEFAULT_LANGUAGE,
) -> str:
    """Upload one Short and return its video id."""
    from googleapiclient.http import MediaFileUpload

    if youtube is None:
        youtube = get_youtube_service()

    body = build_upload_body(
        metadata,
        privacy_status=privacy_status,
        category_id=category_id,
        default_language=default_language,
    )
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response.get("id")
    if not video_id:
        raise RuntimeError(f"YouTube upload returned no video id: {response}")
    return str(video_id)
