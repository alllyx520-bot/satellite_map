"""Exercise GDAL's actual custom opener against bounded local HTTP ranges."""
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import rasterio
from django.test import SimpleTestCase

from .v3.range_reader import BLOCK_SIZE, HTTPRangeFile


class RangeTransportTests(SimpleTestCase):
    def test_real_gdal_decoding_uses_validated_small_ranges(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.tif"
            pixels = np.random.default_rng(27).integers(0, 60000, (512, 512), dtype="uint16")
            with rasterio.open(source, "w", driver="GTiff", width=512, height=512, count=1,
                dtype="uint16", tiled=True, blockxsize=256, blockysize=256,
                crs="EPSG:3857", transform=rasterio.transform.from_origin(0, 5120, 10, 10)) as dst:
                dst.write(pixels, 1)
            content = source.read_bytes()
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    matched = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                    if not matched:
                        self.send_error(400)
                        return
                    first, last = map(int, matched.groups())
                    last = min(last, len(content)-1)
                    requests.append(last-first+1)
                    if last-first+1 > BLOCK_SIZE:
                        self.send_error(413)
                        return
                    body = content[first:last+1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(content)}")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("ETag", '"fixed-version"')
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, format, *args):
                    return
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/source.tif"
                def opener(path, mode="rb"):
                    if Path(path).name != "remote.tif":
                        raise FileNotFoundError(path)
                    return HTTPRangeFile(url)
                with rasterio.open("remote.tif", opener=opener) as remote:
                    actual = remote.read(1, window=((260, 320), (270, 350)))
                np.testing.assert_array_equal(actual, pixels[260:320, 270:350])
                self.assertTrue(requests)
                self.assertLessEqual(max(requests), BLOCK_SIZE)
                self.assertLess(sum(requests), len(content))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
