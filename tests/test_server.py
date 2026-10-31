import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from swipesort.core import Library
from swipesort.server import App, make_handler


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        root = base / "media"
        (root / "sub").mkdir(parents=True)
        (root / "sub/v.mp4").write_bytes(bytes(range(256)) * 4)
        (root / "x.gif").write_bytes(b"GIF89a")
        self.lib = Library(root, base / "swipe.db")
        self.app = App(self.lib, base, None)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.lib._db.close()
        self.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        if data is not None:
            r.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(r)

    def get_json(self, path, body=None):
        with self.req(path, body) as r:
            return json.loads(r.read())

    def test_index_and_static(self):
        with self.req("/") as r:
            self.assertIn(b"swipe-sort", r.read().lower().replace(b"swipe sort", b"swipe-sort"))
        with self.req("/static/app.js") as r:
            self.assertEqual(r.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("/static/../core.py")
        self.assertEqual(cm.exception.code, 404)

    def test_flow(self):
        items = self.get_json("/api/queue")["items"]
        self.assertEqual([i["path"] for i in items], ["sub/v.mp4", "x.gif"])
        self.get_json("/api/decide", {"path": "sub/v.mp4", "decision": "trash"})
        trash = self.get_json("/api/trash")["items"]
        self.assertEqual(trash[0]["path"], "sub/v.mp4")
        with self.req("/api/file?path=sub/v.mp4&trash=1") as r:
            self.assertEqual(len(r.read()), 1024)
        self.assertEqual(self.get_json("/api/undo", {})["item"]["path"], "sub/v.mp4")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("/api/purge", {})
        self.assertEqual(cm.exception.code, 400)

    def test_range(self):
        with self.req("/api/file?path=sub/v.mp4", headers={"Range": "bytes=10-19"}) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.headers["Content-Range"], "bytes 10-19/1024")
            self.assertEqual(r.read(), bytes(range(10, 20)))
        with self.req("/api/file?path=sub/v.mp4", headers={"Range": "bytes=-4"}) as r:
            self.assertEqual(r.read(), bytes(range(252, 256)))

    def test_password(self):
        self.app.password = "pw"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req("/api/queue")
        self.assertEqual(cm.exception.code, 401)
        with self.req("/api/queue", headers={"Authorization": "Basic dTpwdw=="}) as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
