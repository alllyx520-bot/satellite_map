"""证据型外部数据工具包测试：全部 mock external_data 的 requests 与 fetch_cog_bbox_array。"""
from unittest.mock import Mock, patch

import numpy as np
import requests
from django.test import SimpleTestCase

from .agent.durable_tools import _input_scope
from .agent.tools import DEFINITIONS, REGISTRY
from .utils import external_data
from .utils.external_data import (
    query_firms_fires,
    query_landcover_context,
    query_osm_context,
    query_water_baseline,
    query_weather_context,
    summarize_firms_fires,
)

BBOX = {"min_lng": 116.38, "min_lat": 39.90, "max_lng": 116.40, "max_lat": 39.92}

FIRMS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
    "39.91,116.39,330.5,1.0,1.0,2026-09-09,0345,NPP,VIIRS,n,2.0NRT,290.1,12.5,D\n"
    "39.90,116.38,350.2,1.0,1.0,2026-09-09,0345,NPP,VIIRS,h,2.0NRT,300.3,45.8,N\n"
    "39.92,116.40,320.0,1.0,1.0,2026-09-08,1345,NPP,VIIRS,nominal,2.0NRT,280.0,8.1,D\n"
)

OVERPASS_JSON = {
    "elements": [
        {"type": "count", "id": 0, "tags": {"total": "128", "nodes": "0", "ways": "128", "relations": "0"}},
        {"type": "count", "id": 0, "tags": {"total": "45", "nodes": "0", "ways": "45", "relations": "0"}},
        {"type": "count", "id": 0, "tags": {"total": "6", "nodes": "0", "ways": "5", "relations": "1"}},
        {"type": "way", "id": 1, "tags": {"landuse": "residential"}},
        {"type": "way", "id": 2, "tags": {"landuse": "residential"}},
        {"type": "way", "id": 3, "tags": {"landuse": "farmland"}},
    ]
}

OPENMETEO_FORECAST_JSON = {
    "latitude": 39.9, "longitude": 116.4, "timezone": "Asia/Shanghai",
    "daily": {
        "time": ["2026-09-03", "2026-09-04"],
        "temperature_2m_max": [30.1, 28.5],
        "temperature_2m_min": [20.0, 19.2],
        "precipitation_sum": [0.0, 12.4],
        "precipitation_probability_max": [10, 90],
        "weathercode": [1, 63],
    },
}


def _response(text="", json_data=None, status=200):
    resp = Mock(spec=requests.Response)
    resp.status_code = status
    resp.text = text
    resp.json = lambda: json_data
    resp.raise_for_status = Mock()
    if status >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(f"HTTP {status}")
    return resp


def _no_circuit():
    return patch.multiple(
        external_data,
        check_service=Mock(),
        record_failure=Mock(),
        record_success=Mock(),
    )


class FirmsTests(SimpleTestCase):
    def test_csv_parsing_and_summary(self):
        with _no_circuit(), patch.object(external_data.requests, "get", return_value=_response(text=FIRMS_CSV)) as get:
            data = query_firms_fires(BBOX, days=3, api_key="key")
        assert data["count"] == 3
        url = get.call_args.args[0]
        assert "VIIRS_SNPP_NRT" in url and "116.38,39.9,116.4,39.92" in url and url.endswith("/3")
        summary = summarize_firms_fires(data)
        assert summary["fire_count"] == 3
        assert summary["confidence_distribution"] == {"nominal": 2, "high": 1}
        assert summary["top_frp_fires"][0]["frp"] == 45.8
        assert summary["top_frp_fires"][0]["daynight"] == "夜"
        assert "LANCE FIRMS" in summary["attribution"]

    def test_days_clamped_to_five(self):
        with _no_circuit(), patch.object(external_data.requests, "get", return_value=_response(text=FIRMS_CSV)) as get:
            query_firms_fires(BBOX, days=30, api_key="key")
        assert get.call_args.args[0].endswith("/5")

    def test_missing_key_raises_value_error(self):
        with patch.dict("os.environ", {}, clear=True):
            try:
                query_firms_fires(BBOX)
            except ValueError as exc:
                assert "FIRMS_MAP_KEY" in str(exc)
            else:
                raise AssertionError("缺少 key 应抛 ValueError")

    def test_non_csv_response_rejected(self):
        with _no_circuit(), patch.object(external_data.requests, "get", return_value=_response(text="Invalid MAP_KEY")):
            try:
                query_firms_fires(BBOX, api_key="bad")
            except ValueError as exc:
                assert "非 CSV" in str(exc)
            else:
                raise AssertionError("非 CSV 响应应抛 ValueError")


class OsmTests(SimpleTestCase):
    def test_aggregation(self):
        with _no_circuit(), patch.object(external_data.requests, "post", return_value=_response(json_data=OVERPASS_JSON)) as post:
            data = query_osm_context(BBOX)
        assert data["building_count"] == 128
        assert data["road_count"] == 45
        assert data["water_feature_count"] == 6
        assert data["landuse_top"][0] == {"landuse": "residential", "count": 2}
        assert data["landuse_sampled"] == 3
        headers = post.call_args.kwargs.get("headers") or {}
        assert headers.get("User-Agent")


class WeatherTests(SimpleTestCase):
    def test_forecast_series_and_code_mapping(self):
        with _no_circuit(), patch.object(external_data.requests, "get", return_value=_response(json_data=OPENMETEO_FORECAST_JSON)) as get:
            data = query_weather_context(39.9, 116.4)
        assert data["mode"] == "forecast"
        assert get.call_args.kwargs["params"]["past_days"] == 7
        assert data["daily"][0]["weather_desc"] == "晴间多云"
        assert data["daily"][1]["weather_desc"] == "中雨"
        assert "CC-BY-4.0" in data["attribution"]

    def test_archive_uses_date_range(self):
        with _no_circuit(), patch.object(external_data.requests, "get", return_value=_response(json_data=OPENMETEO_FORECAST_JSON)) as get:
            data = query_weather_context(39.9, 116.4, date="2026-08-01")
        assert data["mode"] == "archive"
        assert get.call_args.args[0] == external_data.OPENMETEO_ARCHIVE_URL
        assert get.call_args.kwargs["params"]["start_date"] == "2026-08-01"
        assert get.call_args.kwargs["params"]["end_date"] == "2026-08-01"


GSW_PALETTE = {"80": [0, 0, 150, 255], "30": [100, 200, 255, 255], "0": [255, 255, 150, 255]}
WORLDCOVER_PALETTE = {"40": [255, 255, 100, 255], "50": [195, 40, 40, 255], "10": [0, 100, 0, 255]}


def _rgba_from_values(values2d, palette):
    arr = np.full((*values2d.shape, 4), 255, dtype=np.float32)
    for value, color in palette.items():
        mask = values2d == float(value)
        arr[mask] = color
    return arr


def _palette_mocks(values2d, palette):
    """patch fetch_cog_bbox_array 返回 RGBA,patch requests.get 返回 /cog/info 的 colormap。"""
    rgba = _rgba_from_values(values2d, palette)
    cog_patch = patch.object(external_data, "fetch_cog_bbox_array", return_value=rgba)
    info_patch = patch.object(external_data.requests, "get", return_value=_response(json_data={"colormap": palette}))
    return cog_patch, info_patch


class WaterBaselineTests(SimpleTestCase):
    def test_occurrence_ratios_and_comparison(self):
        # 128 个 >50(常年), 64 个 10-50(季节), 64 个 <10
        values = np.concatenate([np.full(128, 80), np.full(64, 30), np.full(64, 0)]).astype(np.float32).reshape(16, 16)
        cog_patch, colormap_patch = _palette_mocks(values, GSW_PALETTE)
        with cog_patch as fetch, colormap_patch:
            data = query_water_baseline(BBOX, ndwi_ratio=0.9)
        assert data["available"] is True
        assert data["permanent_water_ratio"] == 0.5
        assert data["seasonal_water_ratio"] == 0.25
        assert "高于历史基线" in data["baseline_comparison"]
        url = fetch.call_args.args[0]
        assert "occurrence_110E_40Nv1_4_2021.tif" in url
        assert fetch.call_args.kwargs["kind"] == "palette"
        assert data["tile_partial"] is False

    def test_cross_tile_marks_partial(self):
        values = np.full((16, 16), 80, dtype=np.float32)
        bbox = {"min_lng": 119.9, "min_lat": 39.9, "max_lng": 120.1, "max_lat": 40.1}
        cog_patch, colormap_patch = _palette_mocks(values, GSW_PALETTE)
        with cog_patch, colormap_patch:
            data = query_water_baseline(bbox)
        assert data["tile_partial"] is True
        assert "跨 GSW 瓦片" in data["tile_note"]

    def test_missing_tile_returns_unavailable(self):
        err = requests.exceptions.HTTPError("404")
        with patch.object(external_data, "fetch_cog_bbox_array", side_effect=err):
            data = query_water_baseline(BBOX)
        assert data["available"] is False
        assert "无 GSW 覆盖" in data["reason"]


class LandcoverTests(SimpleTestCase):
    def test_class_mapping_top5(self):
        # 128 耕地(40), 96 建成区(50), 32 林地(10)
        values = np.concatenate([np.full(128, 40), np.full(96, 50), np.full(32, 10)]).astype(np.float32).reshape(16, 16)
        cog_patch, colormap_patch = _palette_mocks(values, WORLDCOVER_PALETTE)
        with cog_patch as fetch, colormap_patch:
            data = query_landcover_context(BBOX)
        assert data["available"] is True
        top = data["landcover_top"]
        assert [item["class"] for item in top] == ["耕地", "建成区", "林地"]
        assert top[0]["percent"] == 50.0
        assert "ESA_WorldCover_10m_2021_v200_N39E114_Map.tif" in fetch.call_args.args[0]
        assert "单年" in data["attribution"]

    def test_missing_tile_returns_unavailable(self):
        with patch.object(external_data, "fetch_cog_bbox_array", side_effect=requests.exceptions.HTTPError("404")):
            data = query_landcover_context(BBOX)
        assert data["available"] is False


class ToolRegistrationTests(SimpleTestCase):
    NAMES = ["query_fire_detections", "query_osm_context", "query_weather_context", "query_water_baseline", "query_landcover_context"]

    def test_registered_with_schema(self):
        for name in self.NAMES:
            assert name in REGISTRY, name
            spec = REGISTRY[name]
            assert spec["description"] and spec["parameters"]["type"] == "object"
            assert callable(spec["fn"])
            DEFINITIONS[name].validate_args({})

    def _ctx(self):
        return {"bbox": BBOX, "slots": {}, "facts": {}}

    def test_fire_tool_missing_key_returns_error(self):
        with patch.dict("os.environ", {}, clear=True):
            result = REGISTRY["query_fire_detections"]["fn"](self._ctx(), {})
        assert result["status"] == "error"
        assert "FIRMS_MAP_KEY" in result["message"]

    def test_fire_tool_ok_shape(self):
        with patch.dict("os.environ", {"FIRMS_MAP_KEY": "k"}), patch.object(
            external_data, "query_firms_fires", return_value={"fires": [], "count": 0, "source": "s", "days": 3}
        ):
            result = REGISTRY["query_fire_detections"]["fn"](self._ctx(), {})
        assert result["status"] == "ok"
        assert result["result"]["fire_count"] == 0
        assert "attribution" in result["result"]

    def test_osm_tool_ok_shape(self):
        with patch.object(external_data, "query_osm_context", return_value={"building_count": 1, "road_count": 2, "water_feature_count": 3, "landuse_top": [], "landuse_sampled": 0, "bbox": BBOX}):
            result = REGISTRY["query_osm_context"]["fn"](self._ctx(), {})
        assert result["status"] == "ok"
        assert result["result"]["attribution"].startswith("OSM 数据")

    def test_weather_tool_derives_center_from_bbox(self):
        captured = {}

        def fake(lat, lng, date=None):
            captured.update({"lat": lat, "lng": lng, "date": date})
            return {"mode": "forecast", "daily": [], "attribution": "x"}

        with patch.object(external_data, "query_weather_context", side_effect=fake):
            result = REGISTRY["query_weather_context"]["fn"](self._ctx(), {})
        assert result["status"] == "ok"
        assert abs(captured["lat"] - 39.91) < 1e-6 and abs(captured["lng"] - 116.39) < 1e-6

    def test_water_baseline_tool_auto_ndwi_and_unavailable(self):
        ctx = self._ctx()
        ctx["ndwi"] = {"available": True, "water_ratio": 0.42}
        seen = {}

        def fake(bbox, ndwi_ratio=None, **kw):
            seen["ndwi_ratio"] = ndwi_ratio
            return {"available": True, "permanent_water_ratio": 0.1, "seasonal_water_ratio": 0.05}

        with patch.object(external_data, "query_water_baseline", side_effect=fake):
            result = REGISTRY["query_water_baseline"]["fn"](ctx, {})
        assert result["status"] == "ok"
        assert seen["ndwi_ratio"] == 0.42
        with patch.object(external_data, "query_water_baseline", return_value={"available": False, "reason": "无 GSW 覆盖"}):
            result = REGISTRY["query_water_baseline"]["fn"](self._ctx(), {})
        assert result["status"] == "failed"

    def test_landcover_tool_unavailable(self):
        with patch.object(external_data, "query_landcover_context", return_value={"available": False, "reason": "无 WorldCover 覆盖"}):
            result = REGISTRY["query_landcover_context"]["fn"](self._ctx(), {})
        assert result["status"] == "failed"

    def test_missing_bbox_returns_error(self):
        for name in self.NAMES:
            result = REGISTRY[name]["fn"]({"slots": {}, "facts": {}}, {})
            assert result["status"] == "error", name


class InputScopeTests(SimpleTestCase):
    def test_fire_scope(self):
        scope = _input_scope("query_fire_detections", {"slots": {}, "bbox": BBOX}, {"days": 2, "source": "s"})
        assert scope == {"bbox": BBOX, "days": 2, "source": "s"}

    def test_osm_scope(self):
        assert _input_scope("query_osm_context", {"slots": {}, "bbox": BBOX}, {}) == {"bbox": BBOX}

    def test_weather_scope(self):
        scope = _input_scope("query_weather_context", {"slots": {}, "bbox": BBOX}, {"date": "2026-08-01"})
        assert scope["date"] == "2026-08-01" and scope["bbox"] == BBOX and "scene_id" not in scope

    def test_water_baseline_scope(self):
        scope = _input_scope("query_water_baseline", {"slots": {}, "bbox": BBOX}, {"ndwi_ratio": 0.3})
        assert scope == {"bbox": BBOX, "ndwi_ratio": 0.3}

    def test_landcover_scope(self):
        assert _input_scope("query_landcover_context", {"slots": {}, "bbox": BBOX}, {}) == {"bbox": BBOX}
