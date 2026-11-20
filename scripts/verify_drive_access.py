"""Check the Drive credentials before a CI run spends minutes on FFmpeg.

Run from the repository root::

    python scripts/verify_drive_access.py

It prints how many raw videos are waiting and never prints a credential.
Exit codes: 0 ready, 1 Drive refused the credentials, 2 configuration missing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drive_sync  # noqa: E402  (after the path fix above)


def main() -> int:
    try:
        folders = drive_sync.load_drive_folders()
    except drive_sync.MissingFolderIdError as exc:
        print(f"[verify] {exc}", file=sys.stderr)
        return 2

    if not (
        str(os.environ.get(drive_sync.ACCESS_TOKEN_ENV, "") or "").strip()
        or str(os.environ.get(drive_sync.TOKEN_JSON_ENV, "") or "").strip()
    ):
        print(
            "[verify] No Drive credentials in the environment: set the "
            f"{drive_sync.TOKEN_JSON_ENV} secret, or start the run from the Apps Script "
            f"trigger so it can pass {drive_sync.ACCESS_TOKEN_ENV}.",
            file=sys.stderr,
        )
        return 2

    try:
        service = drive_sync.build_drive_service()
        videos = drive_sync.list_raw_videos(service, folders.raw)
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        print(f"[verify] Drive access failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"[verify] Drive access OK; {len(videos)} raw video(s) waiting in 01_RAW_VIDEO")
    if not str(os.environ.get("GEMINI_API_KEY", "") or "").strip():
        print(
            "[verify] Warning: GEMINI_API_KEY is empty, so a new recording will stop at the "
            "script step. Add the GEMINI_API_KEY repository secret.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
