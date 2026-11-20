import os
import tempfile
import unittest
from unittest import mock

import video_processor as vp


class FakeFrame:
    def __init__(self, height=1920, width=1080):
        self.shape = (height, width, 3)


class FakeCapture:
    def __init__(self, failures=0, fps=30.0, total=600):
        self.failures = failures
        self.fps = fps
        self.total = total
        self.reads = 0
        self.positions = []
        self.released = False

    def get(self, prop):
        return self.fps if prop == FakeCv2.CAP_PROP_FPS else self.total

    def set(self, prop, position):
        self.positions.append(position)
        return True

    def read(self):
        self.reads += 1
        if self.reads <= self.failures:
            return False, None
        return True, self.frame

    def release(self):
        self.released = True


class FakeCv2:
    CAP_PROP_FPS = 5
    CAP_PROP_FRAME_COUNT = 7
    CAP_PROP_POS_FRAMES = 1
    IMWRITE_JPEG_QUALITY = 2
    INTER_AREA = 3

    def __init__(self, failures=0, fps=30.0, total=600, frame=None, write_ok=True):
        self.capture = FakeCapture(failures=failures, fps=fps, total=total)
        self.capture.frame = frame or FakeFrame()
        self.write_ok = write_ok
        self.written = []
        self.resized = []

    def VideoCapture(self, path):
        self.path = path
        return self.capture

    def imwrite(self, path, frame, params):
        self.written.append((path, params))
        if not self.write_ok:
            return False
        with open(path, "wb") as handle:
            handle.write(b"\xff\xd8fake-jpeg")
        return True

    def resize(self, frame, size, interpolation=None):
        self.resized.append(size)
        return FakeFrame(height=size[1], width=size[0])


class ArtworkCommandTests(unittest.TestCase):
    def test_seeks_from_the_end_and_writes_one_frame(self):
        cmd = vp.build_artwork_command("in.mp4", "out.jpg", offset_seconds=3.0)
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertEqual(cmd[cmd.index("-sseof") + 1], "-3.000")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertEqual(cmd[-1], "out.jpg")
        self.assertIn("-q:v", cmd)

    def test_zero_offset_still_uses_a_legal_seek(self):
        cmd = vp.build_artwork_command("in.mp4", "out.jpg", offset_seconds=0)
        self.assertEqual(cmd[cmd.index("-sseof") + 1], "-0.100")

    def test_invalid_offset_is_clamped(self):
        for bad in [-5, None, "later", float("nan")]:
            cmd = vp.build_artwork_command("in.mp4", "out.jpg", offset_seconds=bad)
            self.assertEqual(cmd[cmd.index("-sseof") + 1], "-0.100")

    def test_quality_is_translated_to_qscale(self):
        best = vp.build_artwork_command("in.mp4", "out.jpg", jpeg_quality=100)
        low = vp.build_artwork_command("in.mp4", "out.jpg", jpeg_quality=50)
        self.assertLess(int(best[best.index("-q:v") + 1]), int(low[low.index("-q:v") + 1]))
        self.assertEqual(int(best[best.index("-q:v") + 1]), 2)


class ExtractArtworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "clip_artwork.jpg")
        self.commands = []

    def _runner(self, cmd):
        self.commands.append(list(cmd))
        with open(cmd[-1], "wb") as handle:
            handle.write(b"\xff\xd8ffmpeg-jpeg")

    def test_cv2_path_saves_the_last_frame(self):
        fake = FakeCv2(total=600, fps=30.0)
        with mock.patch.object(vp, "_import_cv2", return_value=fake):
            result = vp.extract_final_artwork("clip.mp4", self.out, runner=self._runner)
        self.assertEqual(result, self.out)
        self.assertTrue(os.path.getsize(self.out) > 0)
        self.assertEqual(self.commands, [])  # FFmpeg not needed
        self.assertEqual(fake.capture.positions[0], 599)
        self.assertEqual(fake.written[0][1], [FakeCv2.IMWRITE_JPEG_QUALITY, vp.ARTWORK_JPEG_QUALITY])
        self.assertTrue(fake.capture.released)

    def test_steps_back_when_the_tail_frames_do_not_decode(self):
        fake = FakeCv2(total=600, fps=30.0, failures=2)
        with mock.patch.object(vp, "_import_cv2", return_value=fake):
            vp.extract_final_artwork("clip.mp4", self.out, runner=self._runner)
        self.assertEqual(fake.capture.positions[:3], [599, 569, 539])

    def test_offset_moves_the_frame_back(self):
        fake = FakeCv2(total=600, fps=30.0)
        with mock.patch.object(vp, "_import_cv2", return_value=fake):
            vp.extract_final_artwork("clip.mp4", self.out, offset_seconds=3.0, runner=self._runner)
        self.assertEqual(fake.capture.positions[0], 599 - 90)

    def test_wide_frame_is_downscaled_and_portrait_is_left_alone(self):
        wide = FakeCv2(frame=FakeFrame(height=3000, width=4000))
        with mock.patch.object(vp, "_import_cv2", return_value=wide):
            vp.extract_final_artwork("clip.mp4", self.out, runner=self._runner)
        self.assertEqual(wide.resized, [(vp.ARTWORK_MAX_WIDTH, 810)])

        tall = FakeCv2(frame=FakeFrame(height=1920, width=1080))
        with mock.patch.object(vp, "_import_cv2", return_value=tall):
            vp.extract_final_artwork("clip.mp4", self.out, runner=self._runner)
        self.assertEqual(tall.resized, [])

    def test_falls_back_to_ffmpeg_without_cv2(self):
        with mock.patch.object(vp, "_import_cv2", side_effect=ImportError("no cv2")):
            result = vp.extract_final_artwork("clip.mp4", self.out, offset_seconds=2.0, runner=self._runner)
        self.assertEqual(result, self.out)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][self.commands[0].index("-sseof") + 1], "-2.000")

    def test_falls_back_to_ffmpeg_when_the_capture_has_no_frames(self):
        fake = FakeCv2(total=0)
        with mock.patch.object(vp, "_import_cv2", return_value=fake):
            vp.extract_final_artwork("clip.mp4", self.out, runner=self._runner)
        self.assertEqual(len(self.commands), 1)

    def test_raises_when_nothing_was_written(self):
        with mock.patch.object(vp, "_import_cv2", side_effect=ImportError("no cv2")):
            with self.assertRaises(RuntimeError):
                vp.extract_final_artwork("clip.mp4", self.out, runner=lambda cmd: None)

    def test_creates_the_output_directory(self):
        nested = os.path.join(self.tmp.name, "output", "nested", "clip_artwork.jpg")
        fake = FakeCv2()
        with mock.patch.object(vp, "_import_cv2", return_value=fake):
            vp.extract_final_artwork("clip.mp4", nested, runner=self._runner)
        self.assertTrue(os.path.exists(nested))


if __name__ == "__main__":
    unittest.main()
