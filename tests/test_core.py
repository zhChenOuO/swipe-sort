import tempfile
import unittest
from pathlib import Path

from swipesort.core import TRASH_DIR, Library, SwipeError


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "media"
        for rel in ["a.jpg", "2024/b.png", "2024/trip/c.mp4", "2024/trip/d.heic",
                    "2025/e.mov", "notes.txt", "._a.jpg", "@eaDir/x.jpg", "#recycle/y.jpg",
                    ".hidden/z.jpg"]:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(rel.encode())
        self.db = base / "swipe.db"
        self.lib = Library(self.root, self.db)

    def tearDown(self):
        self.lib._db.close()
        self.tmp.cleanup()

    def paths(self, folder=""):
        return [i["path"] for i in self.lib.queue(folder, limit=100)]

    def test_scan_finds_media_in_all_subfolders_and_skips_system_dirs(self):
        self.assertEqual(self.paths(), ["2024/b.png", "2024/trip/c.mp4", "2024/trip/d.heic", "2025/e.mov", "a.jpg"])

    def test_queue_filters_by_folder_including_subfolders(self):
        self.assertEqual(self.paths("2024"), ["2024/b.png", "2024/trip/c.mp4", "2024/trip/d.heic"])
        self.assertEqual(self.paths("2024/trip"), ["2024/trip/c.mp4", "2024/trip/d.heic"])
        # 前綴相同但不是子資料夾的不能算進去
        (self.root / "2024x").mkdir()
        (self.root / "2024x/f.jpg").write_bytes(b"f")
        self.lib.scan()
        self.assertNotIn("2024x/f.jpg", self.paths("2024"))

    def test_queue_cursor(self):
        first = self.lib.queue(limit=2)
        rest = self.lib.queue(after=first[-1]["path"], limit=100)
        self.assertEqual([i["path"] for i in first + rest], self.paths())

    def test_folder_counts(self):
        self.lib.decide("2024/b.png", "keep")
        counts = {f["path"]: (f["pending"], f["total"]) for f in self.lib.folders()}
        self.assertEqual(counts[""], (4, 5))
        self.assertEqual(counts["2024"], (2, 3))
        self.assertEqual(counts["2024/trip"], (2, 2))

    def test_trash_moves_file_and_undo_puts_it_back(self):
        self.lib.decide("2024/trip/c.mp4", "trash")
        self.assertFalse((self.root / "2024/trip/c.mp4").exists())
        self.assertTrue((self.root / TRASH_DIR / "2024/trip/c.mp4").exists())
        self.assertNotIn("2024/trip/c.mp4", self.paths())

        item = self.lib.undo()
        self.assertEqual(item["path"], "2024/trip/c.mp4")
        self.assertEqual(item["undone"], "trash")
        self.assertEqual((self.root / "2024/trip/c.mp4").read_bytes(), b"2024/trip/c.mp4")
        self.assertFalse((self.root / TRASH_DIR / "2024").exists())
        self.assertIn("2024/trip/c.mp4", self.paths())

    def test_undo_is_a_stack(self):
        self.lib.decide("a.jpg", "keep")
        self.lib.decide("2024/b.png", "trash")
        self.lib.decide("2025/e.mov", "keep")
        self.assertEqual([self.lib.undo()["path"] for _ in range(3)], ["2025/e.mov", "2024/b.png", "a.jpg"])
        self.assertIsNone(self.lib.undo())
        self.assertEqual(len(self.paths()), 5)

    def test_state_survives_restart(self):
        self.lib.decide("a.jpg", "keep")
        self.lib.decide("2024/b.png", "trash")
        self.lib._db.close()
        self.lib = Library(self.root, self.db)
        self.assertEqual(self.paths(), ["2024/trip/c.mp4", "2024/trip/d.heic", "2025/e.mov"])
        self.assertEqual(self.lib.undo()["path"], "2024/b.png")
        self.assertTrue((self.root / "2024/b.png").exists())

    def test_trash_name_collision(self):
        self.lib.decide("a.jpg", "trash")
        self.lib.undo()
        (self.root / TRASH_DIR).mkdir(exist_ok=True)
        (self.root / TRASH_DIR / "a.jpg").write_bytes(b"old")
        self.lib.decide("a.jpg", "trash")
        self.assertEqual(self.lib.trash_items()[0]["trash_path"], "a (1).jpg")
        self.lib.restore("a.jpg")
        self.assertEqual((self.root / "a.jpg").read_bytes(), b"a.jpg")
        self.assertEqual((self.root / TRASH_DIR / "a.jpg").read_bytes(), b"old")

    def test_restore_all_and_purge(self):
        for rel in ["a.jpg", "2024/b.png", "2025/e.mov"]:
            self.lib.decide(rel, "trash")
        self.lib.restore("2024/b.png")
        self.assertEqual(self.lib.stats()["trashed"], 2)

        r = self.lib.purge()
        self.assertEqual(r["deleted"], 2)
        self.assertFalse((self.root / TRASH_DIR / "a.jpg").exists())
        self.assertFalse((self.root / TRASH_DIR / "2025").exists())
        self.assertTrue((self.root / "2024/b.png").exists())
        self.assertIsNone(self.lib.undo())
        self.assertEqual(self.lib.stats()["total"], 3)

        self.lib.decide("2024/b.png", "trash")
        self.assertEqual(self.lib.restore_all(), {"restored": 1, "failed": []})

    def test_restore_does_not_overwrite(self):
        self.lib.decide("a.jpg", "trash")
        (self.root / "a.jpg").write_bytes(b"new")
        with self.assertRaises(SwipeError):
            self.lib.undo()
        self.assertEqual((self.root / "a.jpg").read_bytes(), b"new")
        self.assertTrue((self.root / TRASH_DIR / "a.jpg").exists())

    def test_reset_kept(self):
        self.lib.decide("a.jpg", "keep")
        self.lib.decide("2024/b.png", "keep")
        self.assertEqual(self.lib.reset_kept("2024"), 1)
        self.assertIn("2024/b.png", self.paths())
        self.assertNotIn("a.jpg", self.paths())

    def test_keep_all_and_batch_undo(self):
        self.lib.decide("2024/b.png", "trash")
        self.assertEqual(self.lib.decide_all("2024", "keep")["count"], 2)
        self.assertEqual(self.paths(), ["2025/e.mov", "a.jpg"])
        self.assertEqual(self.lib.decide_all("2024", "keep")["count"], 0)
        self.assertEqual(self.lib.undo(), {"undone": "keep", "batch": 2, "failed": []})
        self.assertEqual(self.paths("2024"), ["2024/trip/c.mp4", "2024/trip/d.heic"])
        # 整批撤回後,下一個 undo 是更早的單筆刪除
        self.assertEqual(self.lib.undo()["path"], "2024/b.png")

    def test_trash_all_and_batch_undo(self):
        self.lib.decide("2024/b.png", "keep")
        r = self.lib.decide_all("2024", "trash")
        self.assertEqual(r, {"count": 2, "failed": []})
        self.assertTrue((self.root / TRASH_DIR / "2024/trip/c.mp4").exists())
        self.assertTrue((self.root / "2024/b.png").exists())
        self.assertEqual(self.lib.stats()["trashed"], 2)
        self.assertEqual(self.lib.undo(), {"undone": "trash", "batch": 2, "failed": []})
        self.assertTrue((self.root / "2024/trip/c.mp4").exists())
        self.assertFalse((self.root / TRASH_DIR / "2024").exists())
        with self.assertRaises(SwipeError):
            self.lib.decide_all("2024", "maybe")

    def test_move_and_undo(self):
        r = self.lib.decide("a.jpg", "move", "2024/trip")
        self.assertEqual(r["trash_path"], "2024/trip/a.jpg")
        self.assertFalse((self.root / "a.jpg").exists())
        self.assertTrue((self.root / "2024/trip/a.jpg").exists())
        # 搬過去的不會在目標資料夾再出現一次,重開後也一樣
        self.assertNotIn("2024/trip/a.jpg", self.paths())
        self.lib._db.close()
        self.lib = Library(self.root, self.db)
        self.assertNotIn("2024/trip/a.jpg", self.paths())
        counts = {f["path"]: f["total"] for f in self.lib.folders()}
        self.assertEqual(counts["2024/trip"], 3)

        self.assertEqual(self.lib.undo()["undone"], "move")
        self.assertEqual((self.root / "a.jpg").read_bytes(), b"a.jpg")
        self.assertFalse((self.root / "2024/trip/a.jpg").exists())
        self.assertIn("a.jpg", self.paths())
        self.assertEqual({f["path"]: f["total"] for f in self.lib.folders()}["2024/trip"], 2)

    def test_move_to_new_folder_and_collision(self):
        (self.root / "new").mkdir()
        (self.root / "new/a.jpg").write_bytes(b"other")
        self.assertEqual(self.lib.decide("a.jpg", "move", "new")["trash_path"], "new/a (1).jpg")
        self.assertEqual(self.lib.decide("2025/e.mov", "move", "x/y")["trash_path"], "x/y/e.mov")
        self.assertEqual(self.lib.decide("2024/b.png", "move", "")["trash_path"], "b.png")

    def test_move_rejects_bad_targets(self):
        for bad in ["..", "../out", TRASH_DIR, "2024/@eaDir", "a.jpg", "2024"]:
            with self.subTest(bad=bad), self.assertRaises(SwipeError):
                self.lib.decide("2024/b.png", "move", bad)
        self.assertTrue((self.root / "2024/b.png").exists())

    def test_rejects_bad_paths(self):
        for bad in ["", "../x.jpg", "/etc/passwd", "2024/../a.jpg", f"{TRASH_DIR}/a.jpg", "notes.txt"]:
            with self.subTest(bad=bad), self.assertRaises(SwipeError):
                self.lib.file_path(bad)
        with self.assertRaises(SwipeError):
            self.lib.decide("a.jpg", "maybe")
        self.lib.decide("a.jpg", "keep")
        with self.assertRaises(SwipeError):
            self.lib.decide("a.jpg", "trash")


if __name__ == "__main__":
    unittest.main()
