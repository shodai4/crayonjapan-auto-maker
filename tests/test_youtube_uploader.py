import sys
import types
import unittest
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
