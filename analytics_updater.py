"""Refresh analytics/performance.csv from YouTube.

Views, likes and comments come from the YouTube Data API v3 (together with the
uploaded video's own length). The average view percentage (how much of the Short
people actually watched) and the average view duration (how many seconds that
was) come from the YouTube Analytics API v2, both in one request per video. Any
Analytics failure is logged and the existing CSV value is kept, so a bad request
never wipes the learning history.

The PDCA loop reads this table, so every row also records what the Short *was*
made with (hook style, language, voice mode, target length and the whole
parameter set) and when the numbers were last refreshed.
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

# The columns are append-only: a row written by an older version still reads,
# and a new column is filled with its default instead of breaking the file.
CSV_COLUMNS = [
    "video_id",
    "date",
    "title",
    "style_used",
    "hook_type",
    "views",
    "avg_view_percentage",
    "avg_view_duration_sec",
    "likes",
    "comments_count",
    "language_mode",
    "tts_mode",
    "duration_sec",
    "target_duration_sec",
    "params_json",
    "top_feedback",
    "updated_at",
]
TEXT_COLUMNS = frozenset(
    {
        "video_id",
        "date",
        "title",
        "style_used",
        "hook_type",
        "language_mode",
        "tts_mode",
        "params_json",
        "top_feedback",
        "updated_at",
    }
)

DEFAULT_CSV_PATH = "analytics/performance.csv"
LIST_BATCH_SIZE = 50
DEFAULT_START_DAYS = 90
# One request carries both numbers: the retention and the seconds watched.
ANALYTICS_METRICS = ("averageViewPercentage", "averageViewDuration")
AVG_VIEW_PERCENTAGE = "averageViewPercentage"
AVG_VIEW_DURATION = "averageViewDuration"


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


def fetch_video_details(youtube, video_ids: Sequence[str]) -> Dict[str, dict]:
    """Statistics plus the uploaded length, for up to 50 ids per request.

    The length matters to the loop: a Short that kept people 17 s says one thing
    at 46 s and another at 26 s, so ``duration_sec`` is recorded from YouTube
    itself rather than trusted from the local plan.
    """
    details: Dict[str, dict] = {}
    for batch in chunked(list(video_ids)):
        if not batch:
            continue
        resp = (
            youtube.videos()
            .list(part="statistics,contentDetails", id=",".join(batch))
            .execute()
        )
        for item in resp.get("items", []) or []:
            vid = str(item.get("id"))
            content = item.get("contentDetails") or {}
            details[vid] = {
                "statistics": item.get("statistics", {}) or {},
                "duration_sec": parse_iso_duration(content.get("duration")),
            }
    return details


def parse_iso_duration(value: object) -> Optional[float]:
    """``PT46S`` / ``PT1M3S`` -> seconds (None when unreadable)."""
    text = str(value or "").strip().upper()
    if not text.startswith("PT"):
        return None
    number = ""
    total = 0.0
    matched = False
    for character in text[2:]:
        if character.isdigit() or character == ".":
            number += character
            continue
        if not number:
            continue
        try:
            amount = float(number)
        except ValueError:
            number = ""
            continue
        if character == "H":
            total += amount * 3600
        elif character == "M":
            total += amount * 60
        elif character == "S":
            total += amount
        else:
            return None
        matched = True
        number = ""
    return round(total, 3) if matched else None


def extract_average_view_percentage(response: Optional[dict]) -> Optional[float]:
    """Read averageViewPercentage from a reports().query response."""
    if not response:
        return None
    rows = response.get("rows") or []
    if not rows:
        return None
    values = extract_metric_values(response)
    if AVG_VIEW_PERCENTAGE in values:
        return values[AVG_VIEW_PERCENTAGE]
    # column order follows the requested metrics; the metric is the last cell
    value = rows[0][-1]
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_metric_values(response: Optional[dict]) -> Dict[str, float]:
    """Map a reports().query row onto the metric names it asked for.

    The API answers with a ``columnHeaders`` list; when a (fake or older) reply
    carries only a row, the row is read positionally against the metrics the
    query named.
    """
    if not response:
        return {}
    rows = response.get("rows") or []
    if not rows:
        return {}
    row = rows[0]
    headers = [
        str((header or {}).get("name", ""))
        for header in (response.get("columnHeaders") or [])
    ]
    if not headers or len(headers) != len(row):
        return {}
    values: Dict[str, float] = {}
    for name, value in zip(headers, row):
        try:
            values[name] = float(value)
        except (TypeError, ValueError):
            continue
    return values


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


def fetch_view_metrics(
    analytics,
    video_id: str,
    start_date: str,
    end_date: str,
) -> Optional[Dict[str, float]]:
    """Retention and average view duration for one video, in one request.

    A reply without ``columnHeaders`` (or without rows) still yields the
    retention through the positional fallback, so an API that only answers the
    old metric keeps the loop working.
    """
    query = build_analytics_query(video_id, start_date, end_date, metrics=ANALYTICS_METRICS)
    try:
        response = analytics.reports().query(**query).execute()
    except Exception as exc:  # network, quota, permission, API not enabled...
        log(f"[analytics] skipped {video_id}: {exc}")
        return None
    values = extract_metric_values(response)
    if not values:
        single = extract_average_view_percentage(response)
        if single is None:
            log(f"[analytics] no metrics for {video_id}")
            return None
        return {AVG_VIEW_PERCENTAGE: single}
    return values


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
            df[column] = "" if column in TEXT_COLUMNS else 0
    # A column that is empty in the file is read as float64 (all NaN), which
    # refuses the timestamp/parameter text written later; keep text as objects.
    for column in TEXT_COLUMNS:
        if column in df.columns:
            df[column] = df[column].astype(object)
    # Metrics arrive as fractions (12.4 s), so every number column is a float.
    for column in CSV_COLUMNS:
        if column in TEXT_COLUMNS:
            continue
        df[column] = pd.to_numeric(df[column], errors="coerce").astype(float)
    return df[CSV_COLUMNS]


def _parse_moment(value) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def rows_to_refresh(
    df: pd.DataFrame,
    today: Optional[dt.date] = None,
    min_age_hours: float = 0.0,
    max_rows: Optional[int] = None,
    refresh_hours: float = 6.0,
    now: Optional[dt.datetime] = None,
) -> List:
    """The rows worth asking YouTube about this run.

    With the defaults (``min_age_hours=0``, no cap) every row is refreshed, the
    behaviour the local command had. In the Actions run the loop asks for the
    Shorts that are at least a day old (their numbers have settled), plus any
    row that has never been measured, and never more than ``max_rows`` of them,
    newest first - so a stale row is retried on the next run instead of spending
    the whole quota every 48 hours.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    today = today or now.date()
    if min_age_hours <= 0 and max_rows is None:
        return list(df.index)

    candidates: List = []
    for index, row in df.iterrows():
        vid = str(row.get("video_id", "")).strip()
        if not vid:
            continue
        updated = _parse_moment(row.get("updated_at"))
        if updated is not None:
            age_hours = (now - updated).total_seconds() / 3600.0
            if age_hours < refresh_hours:
                continue
        measured = _to_int(row.get("views"), 0) > 0 or float(
            row.get("avg_view_percentage") or 0
        ) > 0
        published = _as_date(row.get("date"))
        old_enough = True
        if published is not None and min_age_hours > 0:
            age = (today - published).days * 24.0
            old_enough = age >= min_age_hours
        if measured and not old_enough:
            continue
        candidates.append(index)

    if max_rows is not None and len(candidates) > max_rows:
        ordered = sorted(
            candidates,
            key=lambda idx: str(df.at[idx, "date"] or ""),
            reverse=True,
        )
        candidates = ordered[: max(int(max_rows), 1)]
    return candidates


def update_performance_metrics(
    csv_path: str = DEFAULT_CSV_PATH,
    youtube=None,
    analytics=None,
    with_analytics: bool = True,
    today: Optional[dt.date] = None,
    min_age_hours: float = 0.0,
    max_rows: Optional[int] = None,
    refresh_hours: float = 6.0,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """Update the performance CSV in place and return the refreshed frame."""
    df = _ensure_columns(pd.read_csv(csv_path))
    if df.empty:
        print("No videos recorded yet.")
        return df

    video_ids = [str(vid) for vid in df["video_id"].dropna().tolist() if str(vid).strip()]
    if not video_ids:
        return df

    today = today or (now.date() if now else dt.date.today())
    stamp = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0).isoformat()
    targets = rows_to_refresh(
        df, today=today, min_age_hours=min_age_hours,
        max_rows=max_rows, refresh_hours=refresh_hours, now=now,
    )
    target_set = set(targets)

    youtube = youtube or get_youtube_service()
    details = fetch_video_details(youtube, video_ids)
    for idx, row in df.iterrows():
        vid = str(row.get("video_id", "")).strip()
        if vid in details:
            st = details[vid]["statistics"]
            df.at[idx, "views"] = _to_int(st.get("viewCount"), 0)
            df.at[idx, "likes"] = _to_int(st.get("likeCount"), 0)
            df.at[idx, "comments_count"] = _to_int(st.get("commentCount"), 0)
            length = details[vid].get("duration_sec")
            if length:
                df.at[idx, "duration_sec"] = round(float(length), 3)

    if with_analytics and target_set:
        if analytics is None:
            try:
                analytics = get_youtube_analytics_service()
            except Exception as exc:
                log(f"[analytics] client unavailable, keeping CSV values: {exc}")
                analytics = None
        if analytics is not None:
            end_date = today.isoformat()
            for idx in targets:
                row = df.loc[idx]
                vid = str(row.get("video_id", "")).strip()
                if not vid:
                    continue
                start_date = _metric_start_date(_as_date(row.get("date")), today).isoformat()
                values = fetch_view_metrics(analytics, vid, start_date, end_date)
                if not values:
                    continue
                if AVG_VIEW_PERCENTAGE in values:
                    df.at[idx, "avg_view_percentage"] = round(values[AVG_VIEW_PERCENTAGE], 2)
                if AVG_VIEW_DURATION in values:
                    df.at[idx, "avg_view_duration_sec"] = round(values[AVG_VIEW_DURATION], 2)

    if target_set:
        for idx in target_set:
            df.at[idx, "updated_at"] = stamp

    df.to_csv(csv_path, index=False)
    print(f"Updated performance.csv successfully ({len(target_set)} of {len(df)} row(s)).")
    return df


if __name__ == "__main__":
    update_performance_metrics()
