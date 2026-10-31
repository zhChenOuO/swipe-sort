import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from swipesort import transcode
from swipesort.preview import preview


@unittest.skipUnless(transcode.FFMPEG and transcode.FFPROBE, "需要 ffmpeg")
class TranscodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, *codec):
        p = self.base / name
        subprocess.run(
            [transcode.FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
             "-f", "lavfi", "-i", "sine=duration=2", *codec, str(p)],
            check=True,
        )
        return p

    def codecs(self, p):
        r = subprocess.run([transcode.FFPROBE, "-v", "error", "-show_entries", "stream=codec_name",
                            "-of", "json", str(p)], capture_output=True, check=True)
        return [s["codec_name"] for s in json.loads(r.stdout)["streams"]]

    def test_divx_avi_is_reencoded(self):
        src = self.make("a.avi", "-c:v", "mpeg4", "-c:a", "libmp3lame")
        out = transcode.video(src, self.base / "cache")
        self.assertIsNotNone(out)
        self.assertEqual(sorted(self.codecs(out)), ["aac", "h264"])
        self.assertEqual(transcode.video(src, self.base / "cache"), out)

    def test_h264_mkv_is_remuxed(self):
        src = self.make("b.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
        out = transcode.video(src, self.base / "cache")
        self.assertEqual(sorted(self.codecs(out)), ["aac", "h264"])

    def test_broken_file_fails_once(self):
        src = self.base / "c.avi"
        src.write_bytes(b"not a video")
        self.assertIsNone(transcode.video(src, self.base / "cache"))
        self.assertTrue(list((self.base / "cache").rglob("*.fail")))

    def test_poster(self):
        src = self.make("d.avi", "-c:v", "mpeg4", "-an")
        out = preview(src, self.base / "cache", 200)
        self.assertIsNotNone(out)
        self.assertGreater(out.stat().st_size, 0)

    def test_native_formats_skip_transcode(self):
        self.assertFalse(transcode.needs_transcode(Path("x.MP4")))
        self.assertTrue(transcode.needs_transcode(Path("x.avi")))


if __name__ == "__main__":
    unittest.main()
