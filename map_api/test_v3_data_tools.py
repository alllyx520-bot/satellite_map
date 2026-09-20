import numpy as np
from django.test import SimpleTestCase

from .v3.data_tools import DATA_TOOL_SPECS, execute_data_tool


class V3DataToolsTests(SimpleTestCase):
    def test_new_indices_return_screening_product(self):
        bands = {"red": np.full((8, 8), .1), "green": np.full((8, 8), .6),
                 "blue": np.full((8, 8), .1), "nir": np.full((8, 8), .5),
                 "swir": np.full((8, 8), .2), "swir2": np.full((8, 8), .2)}
        for index in ("ndbi", "bsi", "ndsi", "nbr"):
            with self.subTest(index=index):
                response = execute_data_tool("spectral_index", {"index": index, "bands": bands})
                self.assertTrue(response["result"]["available"])
                self.assertEqual(response["result"]["product"], index)

    def test_sar_comparison_requires_same_orbit_and_direction(self):
        result = execute_data_tool("sar_backscatter", {
            "vv": np.full((8, 8), 0.01), "metadata": {"calibration": "linear_sigma0", "relative_orbit": 12, "orbit_direction": "ascending", "incidence_angle_deg": 35},
            "before_metadata": {"relative_orbit": 13, "orbit_direction": "ascending", "incidence_angle_deg": 35},
        })["result"]
        self.assertFalse(result["temporal_comparison_allowed"])
        self.assertGreater(result["water_candidate_mask"].sum(), 0)

    def test_dem_slope_and_aspect_are_derived_in_meter_space(self):
        dem = np.tile(np.arange(8, dtype=np.float32), (8, 1)) * 10
        result = execute_data_tool("dem_terrain", {"dem": dem, "pixel_size_m": [10, 10]})["result"]
        self.assertAlmostEqual(result["summary"]["mean_slope_deg"], 45, places=1)
        self.assertAlmostEqual(result["aspect_deg"][3, 3], 90, places=1)

    def test_landsat_st_masks_cloud_and_applies_c2_scale(self):
        st = np.full((8, 8), 44178, dtype=np.float32)
        qa = np.zeros((8, 8), dtype=np.uint16)
        qa[0, 0] = 1 << 3
        result = execute_data_tool("landsat_surface_temperature", {"st": st, "qa_pixel": qa, "metadata": {"collection": "landsat-c2-l2"}})["result"]
        self.assertEqual(result["summary"]["valid_pixel_count"], 63)
        self.assertAlmostEqual(result["summary"]["mean_celsius"], 26.85, places=1)

    def test_invalid_input_is_structured_error(self):
        result = execute_data_tool("dem_terrain", {"dem": [[1]]})
        self.assertEqual(result["error"]["code"], "invalid_data_input")

    def test_specs_match_dispatcher(self):
        self.assertEqual({spec["name"] for spec in DATA_TOOL_SPECS}, {"spectral_index", "sar_backscatter", "dem_terrain", "landsat_surface_temperature"})
