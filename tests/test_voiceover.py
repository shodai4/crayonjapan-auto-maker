import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import main

CHUNKS = [
    {"type": "audio", "data": b"ID3-audio-head"},
    {"type": "WordBoundary", "offset": 1000000, "duration": 5000000, "text": "Wait"},
    {"type": "audio", "data": b"middle"},
    {"type": "WordBoundary", "offset": 7000000, "duration": 4000000, "text": "for it"},
]


class SubMaker7:
    """edge-tts 7.x: feed(chunk) + get_srt()."""

    def __init__(self):
        self.cues = []

    def feed(self, chunk):
        self.cues.append((chunk["offset"], chunk["duration"], chunk["text"]))

    def get_srt(self):
        return "".join(
            f"{index}\n{start} --> {start}\n{text}\n\n"
            for index, (start, _, text) in enumerate(self.cues, start=1)
        )


class SubMaker6:
    """edge-tts 6.x: create_sub(times, text) + generate_subs()."""

    def __init__(self):
        self.cues = []

    def create_sub(self, times, text):
        self.cues.append((times, text))

    def generate_subs(self):
        return "".join(
            f"{index}\n{times[0]} --> {times[1]}\n{text}\n\n"
            for index, (times, text) in enumerate(self.cues, start=1)
        )


def _fake_module(chunks, calls, supports_boundary=True):
    """A stand-in edge_tts whose Communicate records what it was asked for."""
    module = types.ModuleType("edge_tts")

    class Communicate:
        def __init__(self, text, voice=None, rate=None, boundary=None):
            if boundary is not None and not supports_boundary:
                raise TypeError("Communicate() got an unexpected keyword 'boundary'")
            calls["boundary"] = boundary

        async def stream(self):
            for chunk in chunks:
                yield chunk

    class SubMaker:
        def __init__(self):
            self.texts = []

        def feed(self, chunk):
            self.texts.append(chunk["text"])

        def get_srt(self):
            return "".join(
                f"{index}\n00:00:01,000 --> 00:00:02,000\n{text}\n\n"
                for index, text in enumerate(self.texts, start=1)
            )

    module.Communicate = Communicate
    module.SubMaker = SubMaker
    return module


class SubtitleBoundaryTests(unittest.TestCase):
    """edge-tts 7 defaults to SentenceBoundary, which once left the SRT empty."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio = os.path.join(self.tmp.name, "voice.mp3")
        self.srt = os.path.join(self.tmp.name, "voice.srt")

    def _run(self, module):
        patcher = mock.patch.dict(sys.modules, {"edge_tts": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        asyncio.run(main.synthesize_voice("Wait for it", self.audio, self.srt))
        with open(self.srt, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_word_boundaries_are_requested(self):
        calls = {}
        module = _fake_module(CHUNKS, calls)
        self._run(module)
        self.assertEqual(calls["boundary"], "WordBoundary")

    def test_an_older_communicate_without_the_option_still_runs(self):
        calls = {}
        module = _fake_module(CHUNKS, calls, supports_boundary=False)
        srt = self._run(module)
        self.assertIsNone(calls["boundary"])
        self.assertIn("Wait", srt)

    def test_sentence_boundaries_are_not_dropped(self):
        calls = {}
        chunks = [
            {"type": "audio", "data": b"x"},
            {"type": "SentenceBoundary", "offset": 1000000, "duration": 9000000,
             "text": "Wait for the texture."},
        ]
        srt = self._run(_fake_module(chunks, calls))
        self.assertIn("Wait for the texture.", srt)

    def test_a_blank_srt_is_left_empty_for_the_mux_step_to_handle(self):
        calls = {}
        class Silent:
            def __init__(self):
                pass
            def feed(self, chunk):
                pass
            def get_srt(self):
                return ""
        module = _fake_module(CHUNKS, calls)
        module.SubMaker = Silent
        self.assertEqual(self._run(module), "")


class SynthesizeVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audio_path = os.path.join(self.tmp.name, "voice.mp3")
        self.srt_path = os.path.join(self.tmp.name, "voice.srt")

    def _install_fake_edge_tts(self, submaker_factory):
        module = types.ModuleType("edge_tts")
        calls = {}

        class Communicate:
            def __init__(self, text, voice=None, rate=None):
                calls["text"] = text
                calls["voice"] = voice
                calls["rate"] = rate

            async def stream(self):
                for chunk in CHUNKS:
                    yield chunk

        module.Communicate = Communicate
        module.SubMaker = submaker_factory
        patcher = mock.patch.dict(sys.modules, {"edge_tts": module})
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def _synthesize(self):
        asyncio.run(
            main.synthesize_voice(
                "Wait for it", self.audio_path, self.srt_path, voice="en-US-Test", rate="+10%"
            )
        )

    def test_edge_tts_7_writes_audio_and_srt(self):
        calls = self._install_fake_edge_tts(SubMaker7)

        self._synthesize()

        with open(self.audio_path, "rb") as handle:
            self.assertEqual(handle.read(), b"ID3-audio-headmiddle")
        with open(self.srt_path, "r", encoding="utf-8") as handle:
            srt = handle.read()
        self.assertIn("Wait", srt)
        self.assertIn("for it", srt)
        self.assertEqual(calls["voice"], "en-US-Test")
        self.assertEqual(calls["rate"], "+10%")

    def test_edge_tts_6_still_works(self):
        self._install_fake_edge_tts(SubMaker6)

        self._synthesize()

        with open(self.srt_path, "r", encoding="utf-8") as handle:
            srt = handle.read()
        self.assertIn("1000000 --> 5000000", srt)
        self.assertIn("for it", srt)

    def test_submaker_without_either_api_is_reported(self):
        class Broken:
            def __init__(self):
                pass

        self._install_fake_edge_tts(Broken)

        with self.assertRaises(RuntimeError):
            self._synthesize()

    def test_the_word_timings_are_saved_for_the_telop(self):
        self._install_fake_edge_tts(SubMaker7)
        words_path = os.path.join(self.tmp.name, "voice_words.json")

        asyncio.run(
            main.synthesize_voice(
                "Wait for it", self.audio_path, self.srt_path, words_path=words_path
            )
        )

        with open(words_path, "r", encoding="utf-8") as handle:
            words = json.load(handle)["words"]
        self.assertEqual([word["text"] for word in words], ["Wait", "for it"])
        self.assertAlmostEqual(words[0]["start"], 0.1, places=3)

    def test_requirements_pin_the_new_edge_tts_line(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), "r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle]
        self.assertIn("edge-tts>=7.3.1,<8", lines)


if __name__ == "__main__":
    unittest.main()
