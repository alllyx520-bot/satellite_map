from io import BytesIO
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import tifffile
from PIL import Image
from django.test import SimpleTestCase

from .utils.agent_tools import compute_ndwi_summary, compute_ndwi_mosaic_summary, fetch_cog_bbox_array, polygon_mask_for_bbox


class SpectralAssetTests(SimpleTestCase):
    bbox = {"min_lng": 0, "min_lat": 0, "max_lng": 1, "max_lat": 1}

    def setUp(self):
        self.assets = {name: {"href": f"https://example.test/{name}.tif", "raster:bands": [{"scale": 0.0001, "offset": -0.1, "nodata": 0}]} for name in ["green", "nir", "scl"]}
        self.candidate = SimpleNamespace(assets=self.assets)
        self.arrays = {"green": np.full((64, 64), 6000, dtype=np.uint16), "nir": np.full((64, 64), 2000, dtype=np.uint16), "scl": np.full((64, 64), 6, dtype=np.uint16)}

    def response(self, url, *, params, **kwargs):
        name = params["url"].rsplit("/", 1)[-1].split(".")[0]
        buf = BytesIO()
        Image.fromarray(self.arrays[name]).save(buf, "TIFF")
        return SimpleNamespace(content=buf.getvalue(), raise_for_status=lambda: None)

    def compute(self, **kwargs):
        with patch("map_api.utils.agent_tools.requests.get", side_effect=self.response):
            return compute_ndwi_summary(self.candidate, self.bbox, **kwargs)

    def test_tiff_scale_offset_nodata_and_scl_are_used_in_actual_calculation(self):
        self.arrays["green"][0, 0] = 0
        self.arrays["scl"][1, :8] = [0, 1, 2, 3, 8, 9, 10, 11]
        result = self.compute()
        self.assertTrue(result["available"], result)
        self.assertAlmostEqual(result["mean_ndwi"], 2 / 3, places=4)
        self.assertEqual(result["sample_size_px"], 4096 - 9)
        self.assertEqual(result["mask_source"], "sentinel-2-scl+nodata")
        self.assertEqual(result["water_percent"], 100)

    def test_negative_calibrated_reflectance_does_not_produce_out_of_range_index(self):
        self.arrays["green"][0,0]=500
        self.arrays["nir"][0,0]=1501
        result=self.compute()
        self.assertTrue(result["available"],result)
        self.assertLessEqual(result["max_ndwi"],1)
        self.assertEqual(result["invalid_reflectance_pixel_count"],1)
        self.assertEqual(len(result["alternative_thresholds"]),4)

    def test_missing_scl_fails_before_any_asset_request(self):
        self.assets.pop("scl")
        with patch("map_api.utils.agent_tools.requests.get") as request:
            result = compute_ndwi_summary(self.candidate, self.bbox)
        request.assert_not_called()
        self.assertFalse(result["available"])
        self.assertIn("SCL", result["reason"])

    def test_low_qa_valid_ratio_and_mismatched_scl_cannot_return_a_percentage(self):
        for scl in [np.full((64, 64), 9, dtype=np.uint16), np.full((32, 32), 6, dtype=np.uint16)]:
            self.arrays["scl"] = scl
            result = self.compute()
            self.assertFalse(result["available"])
            self.assertNotIn("water_percent", result)

    def test_unknown_reflectance_scale_is_not_claimed_as_applied(self):
        del self.assets["green"]["raster:bands"][0]["scale"]
        result = self.compute()
        self.assertFalse(result["available"])
        self.assertIn("scale/offset", result["reason"])

    def test_geojson_hole_is_excluded_and_valid_ratio_uses_aoi_denominator(self):
        polygon = {"type": "Polygon", "coordinates": [
            [[0,0],[1,0],[1,1],[0,1],[0,0]],
            [[0.25,0.25],[0.75,0.25],[0.75,0.75],[0.25,0.75],[0.25,0.25]],
        ]}
        mask = polygon_mask_for_bbox(polygon, self.bbox, (64,64))
        self.assertEqual(int(mask.sum()), 3072)
        result = self.compute(polygon=polygon)
        self.assertTrue(result["available"], result)
        self.assertEqual(result["sample_size_px"], 3072)
        self.assertEqual(result["aoi_pixel_count"], 3072)
        self.assertEqual(result["valid_pixel_ratio"], 1)

    def test_rendered_rgb_image_cannot_be_used_as_spectral_band(self):
        buf=BytesIO()
        Image.new("RGB",(64,64),(20,50,90)).save(buf,"TIFF")
        with patch("map_api.utils.agent_tools.requests.get", return_value=SimpleNamespace(content=buf.getvalue(), raise_for_status=lambda:None)):
            with self.assertRaisesRegex(ValueError,"单波段 TIFF"):
                fetch_cog_bbox_array(self.assets["green"],self.bbox,"https://example.test")

    def test_titiler_uint16_data_plus_alpha_preserves_values_and_masks(self):
        values=np.full((64,64,2),65535,dtype=np.uint16)
        values[...,0]=6000
        values[4,5,1]=0
        buf=BytesIO()
        tifffile.imwrite(buf,values,photometric="minisblack",extrasamples=["UNASSALPHA"])
        with patch("map_api.utils.agent_tools.requests.get",return_value=SimpleNamespace(content=buf.getvalue(),raise_for_status=lambda:None)):
            result=fetch_cog_bbox_array(self.assets["green"],self.bbox,"https://example.test")
        self.assertEqual(result.shape,(64,64))
        self.assertTrue(np.isnan(result[4,5]))
        self.assertAlmostEqual(float(result[0,0]),0.5,places=5)

    def mosaic_candidates(self):
        candidates=[]
        for identifier in ("a","b"):
            assets=deepcopy(self.assets)
            for name,asset in assets.items():
                asset["href"]=f"https://example.test/{identifier}/{name}.tif"
            candidates.append(SimpleNamespace(assets=assets,product_id=identifier,acquired_at=datetime(2026,4,1,tzinfo=timezone.utc)))
        return candidates

    def test_same_date_mosaic_counts_overlapping_pixels_once(self):
        with patch("map_api.utils.agent_tools.requests.get",side_effect=self.response):
            result=compute_ndwi_mosaic_summary(self.mosaic_candidates(),self.bbox)
        self.assertTrue(result["available"],result)
        self.assertEqual(result["sample_size_px"],4096)
        self.assertEqual(result["candidate_summaries"][1]["contributed_pixels"],0)
        self.assertFalse(result["change_detection_allowed"])

    def test_complementary_clear_pixels_form_one_same_date_mosaic(self):
        def response(url,*,params,**kwargs):
            identifier,name=params["url"].split("/")[-2:]
            name=name.split(".")[0]
            if name=="scl":
                array=np.full((64,64),9,dtype=np.uint16)
                array[:, :32] = 6 if identifier=="a" else 9
                array[:, 32:] = 9 if identifier=="a" else 6
            else:
                array=np.full((64,64),6000 if (name=="green")==(identifier=="a") else 2000,dtype=np.uint16)
            buf=BytesIO();Image.fromarray(array).save(buf,"TIFF")
            return SimpleNamespace(content=buf.getvalue(),raise_for_status=lambda:None)
        with patch("map_api.utils.agent_tools.requests.get",side_effect=response):
            result=compute_ndwi_mosaic_summary(self.mosaic_candidates(),self.bbox)
        self.assertTrue(result["available"],result)
        self.assertEqual(result["sample_size_px"],4096)
        self.assertEqual(result["water_percent"],50)
        self.assertEqual(result["valid_pixel_ratio"],1)

    def test_cross_date_mosaic_rejected_before_reading_assets(self):
        candidates=self.mosaic_candidates()
        candidates[1].acquired_at=datetime(2026,4,2,tzinfo=timezone.utc)
        with patch("map_api.utils.agent_tools.requests.get") as request:
            result=compute_ndwi_mosaic_summary(candidates,self.bbox)
        request.assert_not_called()
        self.assertFalse(result["available"])
        self.assertIn("跨日期",result["reason"])

    def test_one_failed_mosaic_asset_cannot_return_partial_success(self):
        candidates=self.mosaic_candidates()
        candidates[1].assets.pop("nir")
        with patch("map_api.utils.agent_tools.requests.get",side_effect=self.response):
            result=compute_ndwi_mosaic_summary(candidates,self.bbox)
        self.assertFalse(result["available"])
        self.assertNotIn("water_percent",result)

    def test_ndvi_and_mndwi_use_real_tiff_bands_qa_and_radiometry(self):
        from .spectral_products import compute_spectral_summary
        for name,value in [("red",2000),("swir16",2000)]:
            self.assets[name]=deepcopy(self.assets["green"])
            self.assets[name]["href"]=f"https://example.test/{name}.tif"
            self.arrays[name]=np.full((64,64),value,dtype=np.uint16)
        self.arrays["nir"][:]=6000
        for index in ("ndvi","mndwi"):
            with self.subTest(index=index),patch("map_api.utils.agent_tools.requests.get",side_effect=self.response):
                result=compute_spectral_summary(self.candidate,self.bbox,index=index)
            self.assertTrue(result["available"],result)
            self.assertAlmostEqual(result["mean"],2/3,places=4)
            self.assertEqual(result["thresholded_percent"],100)
            self.assertEqual(result["sample_size_px"],4096)
            self.assertEqual(result["data_contract"]["radiometry"][result["bands"][0]]["scale"],0.0001)
