"""Area-weighting and data-validity regression tests for public background COGs."""
from unittest.mock import patch

import numpy as np
from django.test import SimpleTestCase

from .utils import external_data


def _values(values, palette):
    """Produce the RGBA form consumed by _fetch_palette_values, including nodata."""
    rgba = np.full((*values.shape, 4), 255, dtype=np.float32)
    for code, color in palette.items():
        rgba[values == code] = color
    return rgba


class BackgroundAreaWeightingTests(SimpleTestCase):
    def test_worldcover_two_tiles_with_one_to_three_area_ratio(self):
        # lon 2-3 lies in the first 3° tile; lon 3-6 in the second: 1:3 area.
        bbox = {"min_lng": 2, "min_lat": 0, "max_lng": 6, "max_lat": 1}
        palette = {40: [255, 255, 100, 255], 50: [195, 40, 40, 255]}

        def fetch(_url, clip, *_args, **_kwargs):
            code = 40 if clip["max_lng"] == 3 else 50
            return _values(np.full((16, 16), code), palette)

        with patch.object(external_data, "fetch_cog_bbox_array", side_effect=fetch), patch.object(
            external_data.requests, "get", return_value=type("Response", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"colormap": {str(k): v for k, v in palette.items()}},
            })(),
        ):
            data = external_data.query_landcover_context(bbox)

        top = {entry["class"]: entry["ratio"] for entry in data["landcover_top"]}
        self.assertEqual(data["tile_count"], 2)
        self.assertEqual(top["耕地"], 0.25)
        self.assertEqual(top["建成区"], 0.75)
        self.assertEqual(data["valid_area_ratio"], 1.0)

    def test_water_excludes_invalid_values_and_reports_valid_area(self):
        # Equal-area clips: tile one has 50% valid high-occurrence water;
        # tile two is fully valid no-water.  Valid area is therefore 75%.
        bbox = {"min_lng": 9, "min_lat": 0, "max_lng": 11, "max_lat": 1}
        palette = {80: [0, 0, 150, 255], 0: [255, 255, 150, 255]}

        def fetch(_url, clip, *_args, **_kwargs):
            if clip["max_lng"] == 10:
                values = np.vstack([np.full((8, 16), 80), np.full((8, 16), -999)])
            else:
                values = np.zeros((16, 16))
            return _values(values, palette)

        with patch.object(external_data, "fetch_cog_bbox_array", side_effect=fetch), patch.object(
            external_data.requests, "get", return_value=type("Response", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"colormap": {str(k): v for k, v in palette.items()}},
            })(),
        ):
            data = external_data.query_water_baseline(bbox)

        self.assertEqual(data["valid_area_ratio"], 0.75)
        self.assertAlmostEqual(data["permanent_water_ratio"], 1 / 3, places=4)
        self.assertIn("不能证明常年", data["metric_definitions"]["permanent_water_ratio"])
        self.assertIn("不能单独证明季节性", data["metric_definitions"]["seasonal_water_ratio"])
