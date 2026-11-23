import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

import youtube_uploader as yu


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeInsert:
    def __init__(self, recorder, response):
        self.recorder = recorder
        self.response = response

    def execute(self):
        return self.response


class FakeVideos:
    def __init__(self, recorder, response):
        self.recorder = recorder
        self.response = response

    def insert(self, part=None, body=None, media_body=None):
        self.recorder.append({"part": part, "body": body, "media_body": media_body})
        return FakeInsert(self.recorder, self.response)


class FakeYouTube:
    def __init__(self, response=None):
        self.recorder = []
        self._videos = FakeVideos(self.recorder, response if response is not None else {"id": "vid123"})

    def videos(self):
        return self._videos


class FakeModule(types.ModuleType):
    pass


def fake_googleapiclient():
    http = FakeModule("googleapiclient.http")
    http.MediaFileUpload = lambda *args, **kwargs: {"uploaded": args[0] if args else None}
    package = FakeModule("googleapiclient")
    package.http = http
    return {"googleapiclient": package, "googleapiclient.http": http}


METADATA = {
    "title": "Oil pastel postcard #Shorts",
    "description": "A tiny sunset drawn with Sakura Cray-Pas Specialist.",
    "tags": ["oil pastel", "asmr", "japanese art"],
}


class ScopeTests(unittest.TestCase):
    def test_analytics_scope_is_requested(self):
        self.assertIn("https://www.googleapis.com/auth/yt-analytics.readonly", yu.SCOPES)
        self.assertIn("https://www.googleapis.com/auth/youtube.upload", yu.SCOPES)

    def test_scopes_cover(self):
        self.assertTrue(yu.scopes_cover(list(yu.SCOPES)))
        self.assertFalse(yu.scopes_cover(["https://www.googleapis.com/auth/youtube.upload"]))
        self.assertFalse(yu.scopes_cover([]))

    def test_trailing_slash_does_not_matter(self):
        granted = [scope + "/" for scope in yu.SCOPES]
        self.assertTrue(yu.scopes_cover(granted))


class FakeChannelList:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.payload)


class FakePlaylistItems:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.payload)


class FakeChannelYouTube:
    """The two read calls behind the posting-interval check."""

    def __init__(self, channel_payload, playlist_payload=None):
        self.channels_service = FakeChannelList(channel_payload)
        self.playlist_service = FakePlaylistItems(playlist_payload or {"items": []})

    def channels(self):
        return self.channels_service

    def playlistItems(self):
        return self.playlist_service


class LatestUploadTests(unittest.TestCase):
    def test_the_newest_upload_time_comes_from_the_uploads_playlist(self):
        youtube = FakeChannelYouTube(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
            {"items": [{"snippet": {"publishedAt": "2026-11-21T09:00:00Z"}}]},
        )
        published = yu.get_latest_upload_time(youtube)

        self.assertEqual(published, datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(
            youtube.channels_service.calls[0], {"part": "contentDetails", "mine": True}
        )
        self.assertEqual(
            youtube.playlist_service.calls[0],
            {"part": "snippet", "playlistId": "UU123", "maxResults": 1},
        )

    def test_an_offset_timestamp_is_converted_to_utc(self):
        youtube = FakeChannelYouTube(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
            {"items": [{"snippet": {"publishedAt": "2026-11-21T18:00:00+09:00"}}]},
        )
        self.assertEqual(
            yu.get_latest_upload_time(youtube),
            datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc),
        )

    def test_a_channel_without_any_video_returns_none(self):
        self.assertIsNone(yu.get_latest_upload_time(FakeChannelYouTube({"items": []})))

    def test_no_uploads_playlist_returns_none(self):
        youtube = FakeChannelYouTube({"items": [{"contentDetails": {}}]})
        self.assertIsNone(yu.get_latest_upload_time(youtube))

    def test_an_empty_uploads_playlist_returns_none(self):
        youtube = FakeChannelYouTube(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
            {"items": []},
        )
        self.assertIsNone(yu.get_latest_upload_time(youtube))

    def test_an_unreadable_timestamp_raises_instead_of_guessing(self):
        youtube = FakeChannelYouTube(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
            {"items": [{"snippet": {"publishedAt": "yesterday"}}]},
        )
        with self.assertRaises(ValueError):
            yu.get_latest_upload_time(youtube)

    def test_an_entry_without_a_timestamp_raises(self):
        youtube = FakeChannelYouTube(
            {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}]},
            {"items": [{"snippet": {}}]},
        )
        with self.assertRaises(ValueError):
            yu.get_latest_upload_time(youtube)


class TokenEnvTests(unittest.TestCase):
    """CI has no token.json: the secret carries the JSON itself."""

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.cwd)
        os.chdir(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(yu.TOKEN_JSON_ENV, None)

    def _info(self, expires="2099-01-01T00:00:00Z", token="access-1", refresh="refresh-1"):
        return {
            "token": token,
            "refresh_token": refresh,
            "client_id": "client.apps.googleusercontent.com",
            "client_secret": "secret",
            "scopes": list(yu.SCOPES),
            "expiry": expires,
        }

    def test_without_the_variable_there_are_no_env_credentials(self):
        self.assertIsNone(yu.credentials_from_env())

    def test_the_json_is_read_into_credentials_with_our_scopes(self):
        os.environ[yu.TOKEN_JSON_ENV] = json.dumps(self._info())
        creds = yu.credentials_from_env()

        self.assertEqual(creds.token, "access-1")
        self.assertEqual(creds.refresh_token, "refresh-1")
        self.assertTrue(yu.scopes_cover(creds.scopes))

    def test_a_valid_env_token_is_used_without_a_token_file(self):
        os.environ[yu.TOKEN_JSON_ENV] = json.dumps(self._info())
        creds = yu.load_credentials()

        self.assertEqual(creds.token, "access-1")
        self.assertFalse(os.path.exists(yu.TOKEN_PATH))

    def test_the_env_token_wins_over_an_old_token_file(self):
        with open(yu.TOKEN_PATH, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._info(token="stale", expires="2099-01-01T00:00:00Z")))
        os.environ[yu.TOKEN_JSON_ENV] = json.dumps(self._info(token="fresh"))

        self.assertEqual(yu.load_credentials().token, "fresh")

    def test_an_expired_env_token_is_refreshed_and_written_next_to_it(self):
        from google.oauth2.credentials import Credentials

        os.environ[yu.TOKEN_JSON_ENV] = json.dumps(self._info(expires="2020-01-01T00:00:00Z"))
        with mock.patch.object(Credentials, "refresh") as refresh:
            creds = yu.load_credentials()

        refresh.assert_called_once()
        self.assertEqual(creds.refresh_token, "refresh-1")
        self.assertTrue(os.path.exists(yu.TOKEN_PATH))

    def test_invalid_json_is_reported_with_the_variable_name(self):
        os.environ[yu.TOKEN_JSON_ENV] = "not json at all"
        with self.assertRaises(ValueError) as caught:
            yu.load_credentials()
        self.assertIn(yu.TOKEN_JSON_ENV, str(caught.exception))

    def test_a_json_without_a_refresh_token_is_rejected(self):
        os.environ[yu.TOKEN_JSON_ENV] = json.dumps({"token": "only-short-lived"})
        with self.assertRaises(ValueError) as caught:
            yu.credentials_from_env()
        self.assertIn("refresh_token", str(caught.exception))

    def test_a_missing_client_secret_names_the_environment_variable(self):
        with self.assertRaises(FileNotFoundError) as caught:
            yu.load_credentials()
        self.assertIn(yu.TOKEN_JSON_ENV, str(caught.exception))


class UploadBodyTests(unittest.TestCase):
    def test_body_structure_and_privacy(self):
        body = yu.build_upload_body(METADATA, privacy_status="public")
        snippet = body["snippet"]
        self.assertEqual(snippet["title"], METADATA["title"])
        self.assertEqual(snippet["categoryId"], "22")
        self.assertEqual(snippet["defaultLanguage"], "en")
        self.assertEqual(snippet["tags"], METADATA["tags"])
        self.assertEqual(body["status"]["privacyStatus"], "public")
        self.assertFalse(body["status"]["selfDeclaredMadeForKids"])

    def test_title_is_truncated_to_youtube_limit(self):
        body = yu.build_upload_body({"title": "x" * 150})
        self.assertEqual(len(body["snippet"]["title"]), yu.TITLE_MAX_LENGTH)

    def test_tags_respect_total_length_limit(self):
        body = yu.build_upload_body({"title": "t", "tags": ["tag%03d" % i for i in range(200)]})
        tags = body["snippet"]["tags"]
        self.assertLessEqual(sum(len(tag) + 1 for tag in tags), yu.TAG_MAX_LENGTH)
        self.assertTrue(tags)

    def test_missing_optional_fields_are_safe(self):
        body = yu.build_upload_body({})
        self.assertEqual(body["snippet"]["title"], "")
        self.assertEqual(body["snippet"]["tags"], [])
        self.assertEqual(body["status"]["privacyStatus"], "unlisted")


class UploadShortTests(unittest.TestCase):
    def test_upload_returns_video_id(self):
        youtube = FakeYouTube({"id": "abc123"})
        with mock.patch.dict(sys.modules, fake_googleapiclient()):
            video_id = yu.upload_short("output/clip_final.mp4", METADATA, youtube=youtube)

        self.assertEqual(video_id, "abc123")
        call = youtube.recorder[0]
        self.assertEqual(call["part"], "snippet,status")
        self.assertEqual(call["body"]["snippet"]["title"], METADATA["title"])
        self.assertEqual(call["media_body"]["uploaded"], "output/clip_final.mp4")

    def test_upload_without_id_raises(self):
        youtube = FakeYouTube({})
        with mock.patch.dict(sys.modules, fake_googleapiclient()):
            with self.assertRaises(RuntimeError):
                yu.upload_short("output/clip_final.mp4", METADATA, youtube=youtube)

    def test_privacy_status_is_passed_through(self):
        youtube = FakeYouTube({"id": "abc123"})
        with mock.patch.dict(sys.modules, fake_googleapiclient()):
            yu.upload_short("v.mp4", METADATA, privacy_status="private", youtube=youtube)
        self.assertEqual(youtube.recorder[0]["body"]["status"]["privacyStatus"], "private")


if __name__ == "__main__":
    unittest.main()
