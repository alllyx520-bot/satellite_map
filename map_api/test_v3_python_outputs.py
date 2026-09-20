"""Full-chain coverage for import_python_output and real CJK font rendering."""
import uuid
from pathlib import Path
import tempfile

from django.test import SimpleTestCase, TestCase, override_settings

from .models import Conversation, RunArtifact, SpatialAttachment, SpatialObservation
from .v3.conversations import submit
from .v3.local_python import configure_matplotlib_fonts
from .v3.sandbox import run_python
from .v3.spatial_tools import annotate, finish, import_python_output, python

CHINESE_FIGURE_CODE = """import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.plot([0, 1, 2], [0, 1, 4], label='水体面积')
ax.set_title('哈尔滨春季水体变化')
ax.set_xlabel('日期')
ax.set_ylabel('面积（平方千米）')
ax.legend()
fig.savefig(output_dir / '中文统计图.png', dpi=72)
print('matplotlib_font =', matplotlib_font)
"""

# 产物保留在 MEDIA_ROOT/v3 的沙箱目录；import_python_output 经 assets.adopt_file
# 复制进 v3-assets 附件存储后再注册，metadata.python_import 保留原始溯源。


@override_settings(V3_PYTHON_RUNTIME="local")
class ImportPythonOutputTests(TestCase):
    def setUp(self):
        self.conversation = Conversation.objects.create(owner_session_key="python-output-owner")
        _, self.run = submit(self.conversation.id, "python-output-owner",
                             {"content": "画出中文统计图", "request_id": uuid.uuid4().hex,
                              "attachment_ids": []})
        self.ctx = {"run": self.run, "attachment_ids": [], "version": self.run.context_version,
                    "call_key": "aa00bb01"}

    def _python_chart(self, directory):
        result = python({"code": CHINESE_FIGURE_CODE, "attachment_ids": []}, self.ctx)
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(len(result["artifact_ids"]), 1)
        artifact = RunArtifact.objects.get(pk=result["artifact_ids"][0])
        self.assertTrue((Path(directory) / "v3" / artifact.metadata["relative_path"]).is_file())
        return artifact, result["evidence_ids"]

    def test_import_registers_attachment_observation_and_supports_annotation_and_finish(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            artifact, evidence_ids = self._python_chart(directory)
            response = import_python_output({"artifact_id": artifact.id, "label": "春季水体统计图"}, self.ctx)

            attachment = SpatialAttachment.objects.get(pk=response["attachment"]["id"])
            self.assertEqual(attachment.status, "ready", attachment.error)
            self.assertEqual(attachment.kind, "image")
            self.assertEqual(attachment.conversation_id, self.conversation.id)
            stored = Path(attachment.file_path)
            self.assertTrue(stored.is_file())
            self.assertEqual(stored.parent.parent, Path(directory) / "v3-assets")
            provenance = attachment.metadata["python_import"]
            self.assertEqual(provenance["artifact_id"], artifact.id)
            self.assertEqual(provenance["relative_path"], artifact.metadata["relative_path"])
            self.assertEqual(provenance["sha256"], artifact.metadata["sha256"])
            self.assertEqual(provenance["evidence_refs"], artifact.evidence_refs)

            observation = SpatialObservation.objects.get(pk=response["observation"]["id"])
            self.assertEqual(observation.run_id, self.run.id)
            self.assertEqual(observation.kind, "python_output")
            self.assertEqual(observation.attachment_id, attachment.id)
            self.assertEqual(observation.window, [0, 0, attachment.width, attachment.height])
            self.assertEqual(response["image_refs"], [str(observation.id)])
            self.assertTrue(Path(observation.preview_path).is_file())

            scoped = {**self.ctx, "attachment_ids": [str(attachment.id)], "call_key": "aa00bb02"}
            marked = annotate({"observation_id": str(observation.id), "label": "中文图例",
                               "summary": "图例与坐标轴为中文", "confidence": 0.9}, scoped)
            finding = SpatialObservation.objects.get(pk=marked["observation"]["id"])
            self.assertEqual(finding.kind, "finding")
            self.assertEqual(finding.evidence_refs, [str(observation.id)])

            final = finish({"answer": "统计图展示了面积变化，图例为中文。",
                            "observation_ids": [str(finding.id)],
                            "evidence_ids": evidence_ids, "limitations": []}, scoped)
            self.assertEqual(final["final"]["evidence_ids"], evidence_ids)

    def test_reimport_same_artifact_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            artifact, _ = self._python_chart(directory)
            first = import_python_output({"artifact_id": artifact.id}, self.ctx)
            attachments = SpatialAttachment.objects.count()
            observations = SpatialObservation.objects.count()
            again = import_python_output({"artifact_id": artifact.id, "label": "重复导入"},
                                         {**self.ctx, "call_key": "aa00bb03"})
            self.assertEqual(again["attachment"]["id"], first["attachment"]["id"])
            self.assertEqual(again["observation"]["id"], first["observation"]["id"])
            self.assertEqual(again["image_refs"], first["image_refs"])
            self.assertEqual(SpatialAttachment.objects.count(), attachments)
            self.assertEqual(SpatialObservation.objects.count(), observations)

    def test_rejects_foreign_wrong_kind_and_out_of_root_or_bad_suffix_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            artifact, _ = self._python_chart(directory)
            other_conversation = Conversation.objects.create(owner_session_key="python-output-owner")
            _, other_run = submit(other_conversation.id, "python-output-owner",
                                  {"content": "另一次运行", "request_id": uuid.uuid4().hex,
                                   "attachment_ids": []})
            self.assertNotEqual(other_run.id, self.run.id)
            foreign = RunArtifact.objects.create(run=other_run, artifact_id="python:x:1",
                kind="python_output", title="chart.png", uri="", metadata=dict(artifact.metadata))
            wrong_kind = RunArtifact.objects.create(run=self.run, artifact_id="python:x:2",
                kind="map_export", title="chart.png", uri="", metadata=dict(artifact.metadata))
            escaped = RunArtifact.objects.create(run=self.run, artifact_id="python:x:3",
                kind="python_output", title="x.png", uri="",
                metadata={"relative_path": "../../outside.png"})
            table = Path(directory) / "v3" / "sandbox" / "table"
            table.mkdir(parents=True)
            (table / "结果.csv").write_text("value\n1\n", encoding="utf-8")
            bad_suffix = RunArtifact.objects.create(run=self.run, artifact_id="python:x:4",
                kind="python_output", title="结果.csv", uri="",
                metadata={"relative_path": "sandbox/table/结果.csv"})
            for target in (foreign, wrong_kind, escaped, bad_suffix):
                with self.assertRaises(ValueError, msg=f"artifact {target.artifact_id}"):
                    import_python_output({"artifact_id": target.id}, self.ctx)
            self.assertFalse(SpatialAttachment.objects.exists())
            self.assertFalse(SpatialObservation.objects.exists())


@override_settings(V3_PYTHON_RUNTIME="local")
class ChineseFontRenderingTests(SimpleTestCase):
    def test_configure_matplotlib_fonts_selects_an_installed_cjk_font(self):
        selected = configure_matplotlib_fonts()
        self.assertIsNotNone(selected, "本机应装有 Microsoft YaHei 等中文字体")

    def test_real_subprocess_renders_chinese_figure_without_missing_glyph_warnings(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            result = run_python(CHINESE_FIGURE_CODE, [], "run-9-c0ffee", timeout=120)
            self.assertEqual(result["exit_code"], 0, result.get("error"))
            self.assertNotIn("Glyph", result["stderr"])
            self.assertNotIn("missing from font", result["stderr"])
            chart = Path(directory) / "v3" / result["relative_output_dir"] / "中文统计图.png"
            self.assertTrue(chart.is_file())
            self.assertGreater(chart.stat().st_size, 1000)
