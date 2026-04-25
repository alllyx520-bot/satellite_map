import requests
import os
import math
from PIL import Image
from io import BytesIO
from django.conf import settings

# 云端图片库配置（从环境变量读取，不要硬编码！）
# 比如：CLOUD_API_KEY = os.getenv("CLOUD_SATELLITE_API_KEY")
CLOUD_API_KEY = "你的云端API密钥"  # 替换为实际密钥
CLOUD_API_URL = "你的云端图片库API地址"  # 替换为实际API地址

def haversine_distance(lon1, lat1, lon2, lat2):
    """保留原方法，用于分辨率计算"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def get_cloud_satellite_image(min_lon, min_lat, max_lon, max_lat, save_dir, file_name="cloud_sat_result.jpg", target_resolution=1024, ultra_hd=False):
    """
    云端图片库API调用版本（替换原本地瓦片逻辑）
    """
    try:
        # 1. 复用原分辨率计算逻辑（保证图片比例正确）
        target_resolution = min(1280, max(1, target_resolution))
        lon_diff = max_lon - min_lon
        lat_diff = max_lat - min_lat
        center_lat_rad = math.radians((min_lat + max_lat) / 2.0)
        aspect_ratio = (lon_diff * math.cos(center_lat_rad)) / lat_diff
        if aspect_ratio >= 1:
            width = target_resolution
            height = int(target_resolution / aspect_ratio)
        else:
            height = target_resolution
            width = int(target_resolution * aspect_ratio)
        width = max(1, min(1280, width))
        height = max(1, min(1280, height))
        actual_width = width * 2 if ultra_hd else width
        actual_height = height * 2 if ultra_hd else height

        # 2. 调用云端API（根据云端API的参数要求调整）
        # 示例：主流云端卫星API的参数格式（以高德/天地图/Mapbox为例）
        params = {
            "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "width": actual_width,
            "height": actual_height,
            "apikey": CLOUD_API_KEY,
            # 其他云端要求的参数（如zoom、图层等）
        }
        # 发送请求（加超时，避免卡死）
        response = requests.get(CLOUD_API_URL, params=params, timeout=30)

        # 3. 处理API响应
        if response.status_code == 200:
            # 情况1：云端直接返回图片二进制流
            img = Image.open(BytesIO(response.content))
            # 情况2：云端返回图片URL，需要再下载一次（二选一，根据云端API格式）
            # img_url = response.json().get("img_url")
            # img = Image.open(BytesIO(requests.get(img_url, timeout=30).content))

            # 4. 保存图片到本地media目录（缓存，避免重复调用）
            os.makedirs(save_dir, exist_ok=True)
            full_path = os.path.join(save_dir, file_name)
            img.save(full_path, quality=95)
            print(f"[云端API] ✅ 图片生成成功: {full_path}")
            return full_path
        else:
            print(f"[云端API] ❌ 调用失败，状态码: {response.status_code}, 响应: {response.text}")
            return None

    except requests.exceptions.Timeout:
        print("[云端API] ❌ 请求超时")
        return None
    except requests.exceptions.ConnectionError:
        print("[云端API] ❌ 网络连接失败")
        return None
    except Exception as e:
        print(f"[云端API] ❌ 异常: {str(e)}")
        return None