"""Refresh analytics/performance.csv from YouTube.

Views, likes and comments come from the YouTube Data API v3. The average view
percentage (how much of the Short people actually watch) comes from the YouTube
Analytics API v2, one query per video. Any Analytics failure is logged and the
existing CSV value is kept, so a bad request never wipes the learning history.
"""

from __future__ import annotations

import datetime as dt
import sys
from typing import Dict, Iterable, List, Optional, Sequence

import pandas as pd

from youtube_uploader import (
    build_analytics_query,
    get_youtube_analytics_service,
    get_youtube_service,
)

CSV_COLUMNS = [
    "video_id",
    "date",
    "title",
    "style_used",
    "hook_type",
    "views",
    "avg_view_percentage",
    "likes",
    "comments_count",
    "top_feedback",
]

DEFAULT_CSV_PATH = "analytics/performance.csv"
LIST_BATCH_SIZE = 50
DEFAULT_START_DAYS = 90


def log(message: str) -> None:
    print(message, file=sys.stderr)


def chunked(items: Sequence[str], size: int = LIST_BATCH_SIZE) -> Iterable[List[str]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])


def fetch_video_statistics(youtube, video_ids: Sequence[str]) -> Dict[str, dict]:
    """videos.list statistics for up to 50 ids per request."""
    stats: Dict[str, dict] = {}
    for batch in chunked(list(video_ids)):
        if not batch:
            continue
        resp = youtube.videos().list(part="statistics", id=",".join(batch)).execute()
        for item in resp.get("items", []) or []:
            stats[str(item.get("id"))] = item.get("statistics", {}) or {}
    return stats


def extract_average_view_percentage(response: Optional[dict]) -> Optional[float]:
    """Read averageViewPercentage from a reports().query response."""
    if not response:
        return None
    rows = response.get("rows") or []
    if not rows:
        return None
    # column order follows the requested metrics; the metric is the last cell
    value = rows[0][-1]
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_avg_view_percentage(
    analytics,
    video_id: str,
    start_date: str,
    end_date: str,
) -> Optional[float]:
    """Average view percentage for one video, or None when unavailable."""
    query = build_analytics_query(video_id, start_date, end_date)
    try:
        response = analytics.reports().query(**query).execute()
    except Exception as exc:  # network, quota, permission, API not enabled...
        log(f"[analytics] skipped {video_id}: {exc}")
        return None
    value = extract_average_view_percentage(response)
    if value is None:
        log(f"[analytics] no averageViewPercentage for {video_id}")
        return None
    return value


def _as_date(value) -> Optional[dt.date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        parsed = pd.to_datetime(str(value))
    except (ValueError, TypeError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _metric_start_date(row_date: Optional[dt.date], today: dt.date) -> dt.date:
    fallback = today - dt.timedelta(days=DEFAULT_START_DAYS)
    start = row_date or fallback
    return min(start, today)


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _ensure_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column in CSV_COLUMNS:
        if column not in df.columns:
            df[column] = "" if column in ("title", "style_used", "hook_type", "top_feedback") else 0
    return df[CSV_COLUMNS]


def update_performance_metrics(
    csv_path: str = DEFAULT_CSV_PATH,
    youtube=None,
    analytics=None,
    with_analytics: bool = True,
    today: Optional[dt.date] = None,
) -> pd.DataFrame:
    """Update the performance CSV in place and return the refreshed frame."""
    df = _ensure_columns(pd.read_csv(csv_path))
    if df.empty:
        print("No videos recorded yet.")
        return df

    video_ids = [str(vid) for vid in df["video_id"].dropna().tolist() if str(vid).strip()]
    if not video_ids:
        return df

    today = today or dt.date.today()

    youtube = youtube or get_youtube_service()
    stats_map = fetch_video_statistics(youtube, video_ids)
    for idx, row in df.iterrows():
        vid = str(row.get("video_id", "")).strip()
        if vid in stats_map:
            st = stats_map[vid]
            df.at[idx, "views"] = _to_int(st.get("viewCount"), 0)
            df.at[idx, "likes"] = _to_int(st.get("likeCount"), 0)
            df.at[idx, "comments_count"] = _to_int(st.get("commentCount"), 0)

    if with_analytics:
        if analytics is None:
            try:
                analytics = get_youtube_analytics_service()
            except Exception as exc:
                log(f"[analytics] client unavailable, keeping CSV values: {exc}")
                analytics = None
        if analytics is not None:
            end_date = today.isoformat()
            for idx, row in df.iterrows():
                vid = str(row.get("video_id", "")).strip()
                if not vid:
                    continue
                start_date = _metric_start_date(_as_date(row.get("date")), today).isoformat()
                value = fetch_avg_view_percentage(analytics, vid, start_date, end_date)
                if value is None:
                    continue
                df.at[idx, "avg_view_percentage"] = round(value, 2)

    df.to_csv(csv_path, index=False)
    print("Updated performance.csv successfully.")
    return df


if __name__ == "__main__":
    update_performance_metrics()
