import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
from django.test import TestCase, override_settings
from rasterio.transform import from_origin

from django.utils import timezone

from .models import AgentRun, Conversation, ConversationMessage, RunToolCall, SpatialAttachment
from .v3.data_adapter import execute
from .v3.harness import build_prompt
from .v3.tools import registry


class TwoDateChangeTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.media = override_settings(MEDIA_ROOT=self.temp.name); self.media.enable(); self.addCleanup(self.media.disable)
        self.conversation = Conversation.objects.create(owner_session_key="change-owner", title="change")
        self.run = AgentRun.objects.create(goal="change", conversation=self.conversation, run_key="change-test")

    def _attachment(self, name, red, nir, acquired_at, *, kind="geotiff", collection="sentinel-2-l2a"):
        path = Path(self.temp.name) / name
        data = np.stack([red, nir, np.full(red.shape, 4, dtype="float32")])
        with rasterio.open(path, "w", driver="GTiff", height=red.shape[0], width=red.shape[1], count=3,
                           dtype="float32", crs="EPSG:3857", transform=from_origin(0, 320, 10, 10),
                           tiled=True, blockxsize=16, blockysize=16) as output:
            output.write(data)
        return SpatialAttachment.objects.create(owner_session_key="change-owner", conversation=self.conversation,
            name=name, kind=kind, status="ready", coordinate_space="geographic", file_path=str(path),
            width=red.shape[1], height=red.shape[0], crs="EPSG:3857", transform=list(from_origin(0, 320, 10, 10))[:6],
            bbox={"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}, metadata={"collection": collection,
            "acquired_at": acquired_at, "products": ["ndvi"], "band_map": {"red": 1, "nir": 2, "scl": 3},
            "band_calibration": {"red": {"scale": 1, "offset": 0}, "nir": {"scale": 1, "offset": 0}}})

    def test_change_writes_geotiff_json_evidence_and_locatable_candidates(self):
        red = np.full((32, 32), .2, dtype="float32")
        before = self._attachment("before.tif", red, np.full((32, 32), .3, dtype="float32"), "2026-01-01T10:00:00Z")
        after_nir = np.full((32, 32), .3, dtype="float32"); after_nir[:, 16:] = .8
        after = self._attachment("after.tif", red, after_nir, "2026-02-01T10:00:00Z")
        context = {"conversation": self.conversation, "run": self.run, "version": 1, "call_key": "change-call",
                   "attachment_ids": [str(before.id), str(after.id)]}
        result = execute("compare_two_date_change", {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi", "min_delta": .15}, context)
        self.assertNotIn("error", result)
        outcome = result["result"]
        self.assertGreater(outcome["summary"]["changed_pixel_count"], 400)
        self.assertTrue(outcome["observations"])
        self.assertEqual(self.run.evidence_v2.count(), 1)
        artifacts = list(self.run.artifacts_v2.order_by("id")); self.assertEqual(len(artifacts), 2)
        self.assertEqual(artifacts[0].mime_type, "image/tiff")
        for artifact in artifacts:
            self.assertTrue((Path(self.temp.name) / "v3" / artifact.metadata["relative_path"]).is_file())
        observation = self.conversation.observations.get(pk=outcome["observations"][0])
        self.assertEqual(observation.kind, "change_candidate")
        self.assertEqual(observation.attachment_id, before.id)
        self.assertEqual(observation.geometry["type"], "Polygon")
        self.assertTrue(Path(observation.preview_path).is_file())
        self.assertEqual(outcome["summary"]["reference_grid_pixels_processed"], 1024)
        self.assertEqual(outcome["summary"]["common_valid_pixels"], 1024)
        self.assertIn("candidate_windows_truncated", outcome["summary"])

    def test_change_requires_sent_scope_and_rejects_plain_rgb(self):
        red = np.full((32, 32), .2, dtype="float32")
        before = self._attachment("before.tif", red, np.full((32, 32), .3, dtype="float32"), "2026-01-01T10:00:00Z")
        after = self._attachment("after.tif", red, np.full((32, 32), .4, dtype="float32"), "2026-02-01T10:00:00Z", kind="image")
        context = {"conversation": self.conversation, "run": self.run, "attachment_ids": [str(before.id), str(after.id)]}
        result = execute("compare_two_date_change", {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi"}, context)
        self.assertEqual(result["error"]["code"], "invalid_data_input")
        self.assertIn("普通 RGB", result["error"]["message"])
        context["attachment_ids"] = [str(before.id)]
        result = execute("compare_two_date_change", {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi"}, context)
        self.assertIn("已发送", result["error"]["message"])

    def test_change_requires_reliable_ordered_dates_and_same_collection(self):
        red = np.full((32, 32), .2, dtype="float32")
        before = self._attachment("before.tif", red, np.full((32, 32), .3, dtype="float32"), "2026-01-01T10:00:00Z")
        after = self._attachment("after.tif", red, np.full((32, 32), .4, dtype="float32"), "2026-02-01T10:00:00Z", collection="landsat-c2-l2")
        context = {"conversation": self.conversation, "run": self.run, "attachment_ids": [str(before.id), str(after.id)]}
        result = execute("compare_two_date_change", {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi"}, context)
        self.assertIn("同一受信光学 collection", result["error"]["message"])
        after.metadata["collection"] = "sentinel-2-l2a"; after.metadata["acquired_at"] = "2025-12-01T10:00:00Z"; after.save(update_fields=["metadata"])
        result = execute("compare_two_date_change", {"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi"}, context)
        self.assertIn("早于", result["error"]["message"])

    def test_registry_change_result_builds_harness_images_without_memoryfile(self):
        red = np.full((32, 32), .2, dtype="float32")
        before = self._attachment("before.tif", red, np.full((32, 32), .3, dtype="float32"), "2026-01-01T10:00:00Z")
        after = self._attachment("after.tif", red, np.full((32, 32), .7, dtype="float32"), "2026-02-01T10:00:00Z")
        message = ConversationMessage.objects.create(conversation=self.conversation, run=self.run, role="user",
            content="比较两期影像", status="adopted", sequence=1, request_id="sent-pair", request_digest="fixture")
        message.attachments.set([before, after])
        context = {"conversation": self.conversation, "run": self.run, "version": 1, "call_key": "registry-change",
                   "attachment_ids": [str(before.id), str(after.id)]}
        tool = registry()["compare_two_date_change"]
        with patch("rasterio.io.MemoryFile", side_effect=AssertionError("full TIFF must not enter RAM")):
            result = tool.execute({"reference_attachment_id": str(before.id), "comparison_attachment_id": str(after.id), "product": "ndvi"}, context)
        self.assertTrue(result["image_refs"])
        RunToolCall.objects.create(run=self.run, call_key="registry-change", name=tool.name, step_id="change",
            arguments={}, inputs={}, status="completed", result=result, claim="test", lease_until=timezone.now())
        _, images = build_prompt(self.run, {})
        self.assertTrue(images)
        self.assertTrue(all(item.startswith("data:image/jpeg;base64,") for item in images))
