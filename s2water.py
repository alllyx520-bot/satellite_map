
import json, ssl, urllib.request, numpy as np, rasterio
from rasterio.warp import reproject, Resampling, transform_bounds
from rasterio.windows import from_bounds
from rasterio.crs import CRS

CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
ES = "https://earth-search.aws.element84.com/v1"

def es_search(bbox, dt, limit=400, cols=("sentinel-2-l2a",)):
    payload = {"collections":list(cols), "bbox":bbox, "datetime":dt, "limit":limit}
    req = urllib.request.Request(ES+"/search", data=json.dumps(payload).encode(),
                                headers={"Content-Type":"application/json","User-Agent":"sat-agent/1.0"})
    with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
        return json.loads(r.read().decode())["features"]

def target_grid(aoi, epsg=32652, res=20.0):
    west, south, east, north = transform_bounds(CRS.from_epsg(4326), CRS.from_epsg(epsg), *aoi)
    west, south = np.floor(west/res)*res, np.floor(south/res)*res
    east, north = np.ceil(east/res)*res, np.ceil(north/res)*res
    width = int(round((east-west)/res)); height = int(round((north-south)/res))
    transform = rasterio.transform.from_origin(west, north, res, res)
    return CRS.from_epsg(epsg), transform, width, height

def read_to_grid(href, dst_crs, dst_transform, W, H, resampling=Resampling.bilinear):
    with rasterio.open(href) as src:
        out = np.full((H, W), np.nan, dtype="float32")
        arr = src.read(1).astype("float32")
        nod = src.nodata if src.nodata is not None else 0
        scale = src.scales[0] if src.scales else 1.0
        offset = src.offsets[0] if src.offsets else 0.0
        arr = arr*scale + offset
        arr[arr == nod*scale+offset] = np.nan
        if src.nodata is not None:
            arr[src.read_masks(1) == 0] = np.nan
        reproject(source=arr, destination=out, src_transform=src.transform, src_crs=src.crs,
                  dst_transform=dst_transform, dst_crs=dst_crs, resampling=resampling,
                  src_nodata=np.nan, dst_nodata=np.nan)
        return out
