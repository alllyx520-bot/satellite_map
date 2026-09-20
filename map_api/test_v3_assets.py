import tempfile
import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.test import TestCase, override_settings
from PIL import Image

from .models import Conversation, SpatialAttachment
from .v3 import assets

try:
    import rasterio
    import numpy as np
    RASTERIO = True
except ImportError:
    RASTERIO = False


class AssetPathTests(TestCase):
    def test_inside_uses_captured_root_when_settings_change(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root = Path(first) / "v3-assets"
            root.mkdir()
            target = root / "observations" / "window.jpg"
            with patch.object(assets, "_root", return_value=Path(second) / "v3-assets"):
                self.assertEqual(assets._inside(target, root), target.resolve())


@skipUnless(RASTERIO, "rasterio is required for the large tiled GeoTIFF regression")
class LargeTiledRasterTests(TestCase):
    def _canonical_image(self, directory, name, image, orientation=1):
        path = assets._root() / name
        exif = Image.Exif()
        exif[274] = orientation
        image.save(path, exif=exif)
        attachment = SimpleNamespace(id=uuid.uuid4(), file_path=str(path), metadata={}, bbox=None, sha256='fixture')
        target = assets._canonical(attachment)
        with rasterio.open(target) as dataset:
            return dataset.read(), attachment

    def test_png_exif_orientations_preserve_every_pixel_across_strips(self):
        """Exercise all EXIF mappings with rows on both sides of a 256-row strip."""
        height, width = 513, 7
        source = np.empty((height, width, 3), dtype=np.uint8)
        source[..., 0] = np.arange(height, dtype=np.uint16)[:, None] % 256
        source[..., 1] = np.arange(width, dtype=np.uint8)[None, :]
        source[..., 2] = np.arange(height, dtype=np.uint16)[:, None] // 256
        transforms = {
            1: lambda d: d, 2: lambda d: d[:, ::-1], 3: lambda d: d[::-1, ::-1],
            4: lambda d: d[::-1], 5: lambda d: d.transpose(1, 0, 2),
            6: lambda d: np.rot90(d, -1), 7: lambda d: d.transpose(1, 0, 2)[::-1, ::-1],
            8: lambda d: np.rot90(d, 1),
        }
        centre_maps = {
            1: lambda x, y: (x, y), 2: lambda x, y: (width - x, y),
            3: lambda x, y: (width - x, height - y), 4: lambda x, y: (x, height - y),
            5: lambda x, y: (y, x), 6: lambda x, y: (height - y, x),
            7: lambda x, y: (height - y, width - x), 8: lambda x, y: (y, width - x),
        }
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            for orientation, transform in transforms.items():
                actual, attachment = self._canonical_image(
                    directory, f'orientation-{orientation}.png', Image.fromarray(source), orientation)
                expected = transform(source)
                np.testing.assert_array_equal(np.moveaxis(actual, 0, -1), expected, err_msg=f'orientation {orientation}')
                mapping = attachment.metadata['original_to_image_transform']
                # Transform pixel centres forward and back, including the first,
                # strip-boundary, and final source rows.
                for x, y in ((0, 0), (width - 1, 255), (width // 2, 256), (width - 1, height - 1)):
                    canonical = assets.pixel_to_world(mapping, x + .5, y + .5)
                    self.assertEqual(canonical, centre_maps[orientation](x + .5, y + .5))
                    original = assets.world_to_pixel(mapping, *canonical)
                    self.assertAlmostEqual(original[0], x + .5)
                    self.assertAlmostEqual(original[1], y + .5)

    def test_palette_and_alpha_png_are_canonicalized_to_visible_rgb(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            rgba = np.array([[[200, 100, 50, 128], [10, 20, 30, 255]]], dtype=np.uint8)
            rgb, _ = self._canonical_image(directory, 'alpha.png', Image.fromarray(rgba, 'RGBA'))
            np.testing.assert_array_equal(np.moveaxis(rgb, 0, -1), np.array([[[100, 50, 25], [10, 20, 30]]], dtype=np.uint8))

            palette = Image.fromarray(np.array([[0, 1]], dtype=np.uint8), 'P')
            palette.putpalette([200, 100, 50, 10, 20, 30] + [0] * (256 * 3 - 6))
            palette.info['transparency'] = bytes([128, 255])
            rgb, _ = self._canonical_image(directory, 'palette.png', palette)
            np.testing.assert_array_equal(np.moveaxis(rgb, 0, -1), np.array([[[100, 50, 25], [10, 20, 30]]], dtype=np.uint8))

    def test_upload_limits_and_path_boundaries(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with self.assertRaises(ValueError):
                assets.create_upload('owner', 'sample.png', True)
            with self.assertRaises(ValueError):
                assets.create_upload('owner', 'sample.png', assets.MAX_UPLOAD_BYTES + 1)
            self.assertEqual(assets._safe_name('../nested/sample.png'), 'sample.png')
            with self.assertRaises(ValueError):
                assets._safe_name('x' * 241 + '.png')
            with self.assertRaises(ValueError):
                assets._inside(Path(directory).parent / 'outside.png')
            upload = assets.create_upload('owner', 'sample.png', 1)
            with self.assertRaises(ValueError):
                assets.upload_chunk(upload.id, 'owner', True, io.BytesIO(b'x'))

    def test_processing_does_not_overwrite_concurrent_conversation_assignment(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            path = assets._root() / 'sample.png'
            Image.new('RGB', (16, 12), (30, 60, 90)).save(path)
            attachment = SpatialAttachment.objects.create(owner_session_key='owner', name='sample.png',
                status='pending', file_path=str(path))
            conversation = Conversation.objects.create(owner_session_key='owner')
            original = assets._canonical
            def during_processing(stale):
                SpatialAttachment.objects.filter(pk=stale.pk).update(conversation=conversation, name='用户命名')
                return original(stale)
            with patch.object(assets, '_canonical', side_effect=during_processing):
                ready = assets.process_attachment(attachment.pk)
            self.assertEqual(ready.status, 'ready')
            self.assertEqual(ready.conversation_id, conversation.id)
            self.assertEqual(ready.name, '用户命名')

    def test_20000px_geotiff_window_is_decimated_without_full_read(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            path = assets._root() / "files" / "large.tif"
            path.parent.mkdir(parents=True, exist_ok=True)
            # This writes two 256px blocks only; it deliberately never allocates a
            # 20,000² image in memory.
            with rasterio.open(path, "w", driver="GTiff", width=20000, height=20000, count=1,
                               dtype="uint8", tiled=True, blockxsize=256, blockysize=256, compress="lzw") as dst:
                dst.write(np.full((256, 256), 127, dtype="uint8"), 1, window=((0, 256), (0, 256)))
                dst.write(np.full((256, 256), 255, dtype="uint8"), 1, window=((19744, 20000), (19744, 20000)))
            conversation = Conversation.objects.create(owner_session_key="owner", title="test")
            attachment = SpatialAttachment.objects.create(owner_session_key="owner", conversation=conversation,
                name="large.tif", kind="geotiff", status="processing", file_path=str(path), size_bytes=path.stat().st_size)
            ready = assets.process_attachment(attachment.id)
            self.assertEqual(ready.status, "ready", ready.error)
            self.assertEqual((ready.width, ready.height), (20000, 20000))
            item = assets.read_window(ready, 19744, 19744, 256, 256, max_size=128)
            self.assertEqual(item["window"], [19744, 19744, 256, 256])
            self.assertTrue(Path(item["path"]).is_file())
            # The second written block must remain visible after decimation.
            self.assertGreater(np.asarray(Image.open(item["path"])).mean(), 240)

    def test_chunk_integrity_and_owner_isolation(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            upload = assets.create_upload("one", "sample.png", 3)
            assets.upload_chunk(upload.id, "one", 0, io.BytesIO(b"abc"))
            with self.assertRaises(ValueError):
                assets.upload_chunk(upload.id, "one", 0, io.BytesIO(b"abd"))
            with self.assertRaises(LookupError):
                assets.complete_upload(upload.id, "two")
            part = assets._root() / "uploads" / str(upload.id) / "0"
            part.write_bytes(b"bad")
            with self.assertRaises(ValueError):
                assets.complete_upload(upload.id, "one")

    def test_rotated_affine_round_trip(self):
        transform = [2.0, 0.5, 100.0, -0.25, -3.0, 50.0]
        world = assets.pixel_to_world(transform, 71.25, 83.5)
        pixel = assets.world_to_pixel(transform, *world)
        self.assertAlmostEqual(pixel[0], 71.25, places=6)
        self.assertAlmostEqual(pixel[1], 83.5, places=6)
