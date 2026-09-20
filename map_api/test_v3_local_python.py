"""Real local Python execution, including network, artifacts and interruption."""
import json
from pathlib import Path
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import numpy as np
import rasterio
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from rasterio.transform import from_origin

from .models import Conversation, RunArtifact
from .v3.conversations import submit
from .v3.harness import execute_run
from .v3.local_python import configure_matplotlib_fonts
from .v3.runtime import ToolInterrupted, tool_budget
from .v3.sandbox import file_hash, run_python, sandbox_status
from .v3.tools import load_capabilities, model_tools, registry


@override_settings(V3_PYTHON_RUNTIME="local")
class LocalPythonTests(SimpleTestCase):
    def test_local_status_and_analysis_tools_do_not_probe_docker(self):
        with patch("map_api.v3.sandbox._docker_status", side_effect=AssertionError("Docker must not be called")):
            state = sandbox_status()
            loaded = load_capabilities({"groups": ["analysis"]}, {})
            offered = model_tools(registry(), loaded)
        self.assertEqual(state["runtime"], "local")
        self.assertTrue(state["available"])
        self.assertFalse(state["isolated"])
        self.assertTrue(state["network"])
        self.assertIn("python_analysis", loaded["enabled_tools"])
        self.assertIn("python_analysis", {item["function"]["name"] for item in offered})

    def test_matplotlib_font_configuration_uses_only_installed_fonts(self):
        selected = configure_matplotlib_fonts()
        self.assertTrue(selected is None or isinstance(selected, str))

    def test_real_raster_calculation_and_hashed_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            image = Path(directory) / "带 空格的影像.tif"
            pixels = np.arange(16, dtype="float32").reshape(4, 4)
            with rasterio.open(image, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
                               crs="EPSG:32650", transform=from_origin(0, 40, 10, 10)) as dst:
                dst.write(pixels, 1)
            code = """from __future__ import annotations
import json
import rasterio
with rasterio.open(inputs[0]) as src:
    mean = float(src.read(1).mean())
(output_dir / '统计.json').write_text(json.dumps({'mean': mean}), encoding='utf-8')
print('真实均值', mean)
"""
            result = run_python(code, [image], "run-1-a")
            target = Path(directory) / "v3" / result["relative_output_dir"] / "统计.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"mean": 7.5})
            self.assertEqual(result["outputs"][0]["sha256"], file_hash(target))
            self.assertEqual(result["inputs"][0]["sha256"], file_hash(image))
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["runtime"], "local")
            self.assertIn("真实均值", result["stdout"])
            metadata = target.parent.parent / "control" / "execution.json"
            self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["runtime"], "local")

    def test_local_mode_can_write_outside_artifact_directory(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            target = Path(directory) / "development-output.txt"
            result = run_python(f"from pathlib import Path\nPath({str(target)!r}).write_text('local')\nprint(project_dir.is_dir())", [], "run-6-f")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(target.read_text(), "local")
            self.assertIn("True", result["stdout"])

    def test_network_and_subprocess_are_available(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"value":42}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
                code = f"""import json, subprocess, sys, urllib.request
with urllib.request.urlopen('http://127.0.0.1:{server.server_port}/', timeout=3) as response:
    value = json.load(response)['value']
child = subprocess.run([sys.executable, '-c', 'print(6*7)'], capture_output=True, text=True, check=True)
print(value, child.stdout.strip())
"""
                result = run_python(code, [], "run-2-b")
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("42 42", result["stdout"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_code_error_and_large_logs_return_debuggable_results(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            result = run_python("print('x'*200000)\nraise ValueError('invalid sample')", [], "run-3-c")
            self.assertNotEqual(result["exit_code"], 0)
            self.assertEqual(result["error"]["code"], "python_error")
            self.assertIn("invalid sample", result["error"]["message"])
            self.assertLessEqual(len(result["stdout"]), 64000)
            self.assertTrue(result["stdout_truncated"])

    def test_timeout_stops_process_tree(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            marker = Path(directory) / "child-completed.txt"
            child = f"import time; from pathlib import Path; time.sleep(3); Path({str(marker)!r}).write_text('late')"
            code = f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(30)"
            started = time.monotonic()
            result = run_python(code, [], "run-4-d", timeout=1)
            self.assertTrue(result["timed_out"])
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(3)
            self.assertFalse(marker.exists())

    def test_task_cancellation_stops_local_execution(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            checks = 0

            def cancelled():
                nonlocal checks
                checks += 1
                if checks > 1:
                    raise ToolInterrupted("用户停止")

            started = time.monotonic()
            with self.assertRaisesMessage(ToolInterrupted, "用户停止"), tool_budget(10, cancelled):
                run_python("import time\ntime.sleep(30)", [], "run-5-e")
            self.assertLess(time.monotonic() - started, 5)
            audit = Path(directory) / "v3/sandbox/run-5-e/control/execution.json"
            self.assertEqual(json.loads(audit.read_text(encoding="utf-8"))["status"], "interrupted")


@override_settings(V3_PYTHON_RUNTIME="local")
class LocalPythonHarnessTests(TransactionTestCase):
    def test_harness_executes_local_code_and_publishes_downloadable_artifact(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            conversation = Conversation.objects.create(owner_session_key="local-execution-test")
            _, run = submit(conversation.id, conversation.owner_session_key,
                            {"content": "计算 6×7 并保存为表格", "attachment_ids": [], "request_id": uuid.uuid4().hex})

            def call(name, args):
                return {"id": uuid.uuid4().hex, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

            replies = iter([
                {"tool_calls": [call("load_capabilities", {"groups": ["analysis"]})]},
                {"tool_calls": [call("python_analysis", {"code": "import json\n(output_dir / 'result.csv').write_text('value\\n42\\n')\n(output_dir / 'analysis_result.json').write_text(json.dumps({'answer': {'value': 42, 'unit': 'count', 'scope_id': 'all'}}), encoding='utf-8')\nprint(6*7)", "attachment_ids": []})]},
            ])
            def provider(*_args, **_kwargs):
                response = next(replies, None)
                if response:
                    return response
                return {"tool_calls": [call("finish_answer", {"answer": "计算结果为 42，表格已生成。", "observation_ids": [],
                    "evidence_ids": [run.evidence_v2.get(metric="python_analysis").evidence_id], "limitations": []})]}

            self.assertTrue(execute_run(run.id, provider=provider))
            run.refresh_from_db()
            self.assertEqual(run.status, "completed")
            tool = run.tool_calls.get(name="python_analysis")
            self.assertEqual(tool.result["runtime"], "local")
            self.assertNotIn("error", tool.result)
            self.assertEqual(tool.result["evidence_ids"], [run.evidence_v2.get().evidence_id])
            evidence = run.evidence_v2.get()
            self.assertEqual(evidence.value["execution_status"], "completed")
            self.assertEqual(evidence.value["result"]["answer"]["value"], 42)
            artifact = RunArtifact.objects.get(run=run)
            self.assertEqual(artifact.metadata["runtime"], "local")
            self.assertEqual(artifact.metadata["execution_status"], "completed")
            self.assertEqual(artifact.mime_type, "text/csv")
            self.assertEqual(artifact.evidence_refs, tool.result["evidence_ids"])
            self.assertIn({"type": "evidence_ref", "id": tool.result["evidence_ids"][0]},
                          conversation.messages.get(role="assistant").parts)
            path = Path(directory) / "v3" / artifact.metadata["relative_path"]
            self.assertEqual(path.read_text(), "value\n42\n")
