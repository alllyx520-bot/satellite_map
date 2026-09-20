"""Optional Docker runtime must not prevent ordinary V3 operation."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from .v3.tools import load_capabilities, model_tools, registry


@override_settings(V3_PYTHON_RUNTIME="docker")
class OptionalRuntimeTests(TestCase):
    def test_non_sandbox_groups_do_not_probe_docker(self):
        with patch("map_api.v3.sandbox.sandbox_status") as status:
            result = load_capabilities({"groups": ["imagery", "context"]}, {})
        status.assert_not_called()
        self.assertIn("compute_product", result["enabled_tools"])
        self.assertIn("external_evidence", result["enabled_tools"])

    def test_analysis_group_keeps_vision_and_reports_missing_sandbox(self):
        unavailable = {"available": False, "detail": "Docker 服务或分析镜像不可用"}
        with patch("map_api.v3.sandbox.sandbox_status", return_value=unavailable):
            result = load_capabilities({"groups": ["analysis"]}, {})
        self.assertEqual(result["enabled_tools"], ["review_visual"])
        self.assertEqual(result["unavailable_tools"], {"python_analysis": unavailable["detail"],
                                                       "import_python_output": unavailable["detail"]})

    def test_unavailable_python_is_never_offered_to_controller(self):
        unavailable = {"available": False, "detail": "Docker 服务不可用"}
        with patch("map_api.v3.sandbox.sandbox_status", return_value=unavailable):
            offered = model_tools(registry(), {"enabled_tools": ["review_visual", "python_analysis", "import_python_output"]})
        names = {item["function"]["name"] for item in offered}
        self.assertIn("review_visual", names)
        self.assertNotIn("python_analysis", names)
        self.assertNotIn("import_python_output", names)

    def test_capabilities_marks_python_as_unavailable_without_hiding_core_tools(self):
        unavailable = {"available": False, "detail": "Docker 服务或分析镜像不可用"}
        with patch("map_api.v3.sandbox.sandbox_status", return_value=unavailable):
            response = self.client.get("/api/v3/capabilities/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        specs = {item["name"]: item for item in payload["tools"]}
        self.assertFalse(specs["python_analysis"]["available"])
        self.assertEqual(specs["python_analysis"]["unavailable_reason"], unavailable["detail"])
        self.assertIn("view_overview", specs)
