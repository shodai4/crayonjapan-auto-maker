"""YouTube upload (Data API v3) and read access for YouTube Analytics API v2.

One OAuth token is reused for both APIs, so the requested scopes already cover
the Analytics read used to learn from average view percentage.
"""

from __future__ import annotations

import os
from typing import List, Optional, Sequence

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

TOKEN_PATH = "token.json"
CLIENT_SECRET_PATH = "client_secret.json"

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


def load_credentials(force_authorize: bool = False):
    """Load cached OAuth credentials, refreshing or re-authorizing as needed.

    The Analytics scope was added later, so a token issued before that gets
    re-authorized instead of failing halfway through a run.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(TOKEN_PATH) and not force_authorize:
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
                    "in the project root."
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
    """Assemble the videos.insert body, trimmed to YouTube's field limits."""
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
