from django.http import JsonResponse, FileResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.shortcuts import render
from http import HTTPStatus
import dashscope
import json
import os
import uuid
import math
import requests

from .utils.get_satellite_image import fetch_satellite_image, haversine_distance

# 规范化保存目录
SAVE_DIR = os.path.join(settings.MEDIA_ROOT, 'satellite_imgs')
if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

def index_view(request):
    """负责展示前端地图页面"""
    return render(request, 'browser.html')

# ----------------------
# 卫星图下载接口
# ----------------------
@csrf_exempt
def get_satellite_img_api(request):
    if request.method != 'POST':
        return JsonResponse({'code':405,'msg':'仅支持POST','data':None},status=405)

    try:
        data = json.loads(request.body)
        min_lng = float(data['min_lng'])
        min_lat = float(data['min_lat'])
        max_lng = float(data['max_lng'])
        max_lat = float(data['max_lat'])
        resolution = int(data.get('target_resolution', 0))

        # 根据场景大小自动推算分辨率：大范围用高分辨率（拼接），小范围保持适中
        lon_span = max_lng - min_lng
        lat_span = max_lat - min_lat
        center_lat_rad = math.radians((min_lat + max_lat) / 2)
        diag_m = haversine_distance(min_lng, min_lat, max_lng, max_lat)
        if resolution <= 0:
            if diag_m > 20000:
                resolution = 3072
            elif diag_m > 5000:
                resolution = 2048
            else:
                resolution = 1280

        file_name = f"sat_{uuid.uuid4().hex[:8]}.jpg"

        img_path = fetch_satellite_image(
            min_lng, min_lat, max_lng, max_lat,
            save_dir=SAVE_DIR,
            file_name=file_name,
            target_resolution=resolution
        )

        if img_path:
            # 空间上下文：像素分辨率 & 面积估算
            gsd_lon = (lon_span / resolution) * 111320 * math.cos(center_lat_rad)
            gsd_lat = (lat_span / (resolution / ((lon_span * math.cos(center_lat_rad)) / lat_span if lat_span else 1))) * 110574
            gsd = (gsd_lon + gsd_lat) / 2
            area_km2 = (lon_span * 111320 * math.cos(center_lat_rad)) * (lat_span * 110574) / 1e6
            return JsonResponse({
                "code":200,
                "msg":"卫星图生成成功",
                "data":{
                    "save_path": img_path,
                    "file_name": file_name,
                    "resolution_px": resolution,
                    "gsd_m": round(gsd, 2),
                    "area_km2": round(area_km2, 4),
                }
            })
        else:
            return JsonResponse({"code":500,"msg":"获取失败","data":None},status=500)

    except Exception as e:
        return JsonResponse({"code":400,"msg":str(e),"data":None},status=400)

# ----------------------
# 精准读取图片接口 (防缓存、防串联)
# ----------------------
def show_satellite_image(request):
    file_name = request.GET.get('file')
    if file_name:
        target_path = os.path.join(SAVE_DIR, file_name)
        if not os.path.exists(target_path):
             return JsonResponse({"code": 404, "msg": "找不到指定的卫星图"}, status=404)
    else:
        # 兼容旧版的后备逻辑
        files = sorted([f for f in os.listdir(SAVE_DIR) if f.endswith('.jpg')],
                        key=lambda x: os.path.getmtime(os.path.join(SAVE_DIR, x)), reverse=True)
        if not files:
            return JsonResponse({"code":404,"msg":"无图片"},status=404)
        target_path = os.path.join(SAVE_DIR, files[0])
        
    return FileResponse(open(target_path,'rb'), content_type='image/jpeg')

# ----------------------
# AI 多轮视觉推理接口 (Qwen3.5-Plus)
# ----------------------
@csrf_exempt
def ai_query_region(request):
    try:
        data = json.loads(request.body)
        file_name = data.get("file_name")
        file_names = data.get("file_names")
        question = data.get("question")
        front_history = data.get("history", [])

        dashscope.api_key = os.environ.get('DASHSCOPE_API_KEY', '')

        SYSTEM_PROMPT = """你是SatelliteSense，顶级的遥感图像分析专家。精通地理学、城市规划、农学、水文学等多领域。

## 回答原则
- 专业客观，使用遥感标准术语
- 用数据说话（如"约30%植被覆盖"）
- 根据问题灵活组织：开放性分析可分段阐述，具体问题直接精准回答
- 中文回答，简洁有力"""

        # 多图对比
        if file_names and isinstance(file_names, list) and len(file_names) >= 2:
            labels = "ABCDEFGH"
            content_parts = []
            for i, fn in enumerate(file_names):
                fp = os.path.join(SAVE_DIR, fn)
                if os.path.exists(fp):
                    label = labels[i] if i < len(labels) else f"区域{i+1}"
                    content_parts.append({"image": f"file://{fp}"})
                    content_parts.append({"text": f"这是区域 {label}。"})
            if len([p for p in content_parts if "image" in p.values()]) < 2:
                return JsonResponse({"code": 400, "msg": "至少需要 2 张有效图片", "data": None})
            content_parts.append({"text": f"{SYSTEM_PROMPT}\n\n共有 {len(file_names)} 个区域的遥感卫星影像，请对比分析。"})
            messages = [{"role": "user", "content": content_parts}]
        # 单图
        else:
            if not file_name:
                return JsonResponse({"code": 400, "msg": "缺少图片标识", "data": None})
            target_path = os.path.join(SAVE_DIR, file_name)
            if not os.path.exists(target_path):
                return JsonResponse({"code": 404, "msg": "卫星图文件已丢失，请重新框选", "data": None})

            spatial_ctx = data.get("spatial_context", "")
            first_msg = SYSTEM_PROMPT + "\n\n---\n\n请分析这张遥感卫星影像。"
            if spatial_ctx:
                first_msg += "\n\n## 空间上下文（供参考）\n" + spatial_ctx

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"image": f"file://{target_path}"},
                        {"text": first_msg}
                    ]
                }
            ]

        for msg in front_history:
            role = "user" if msg["role"] == "user" else "assistant"
            messages.append({"role": role, "content": [{"text": msg["content"]}]})

        messages.append({
            "role": "user",
            "content": [{"text": question}]
        })

        response = dashscope.MultiModalConversation.call(
            model='qwen3.5-plus',
            messages=messages
        )

        if response.status_code == HTTPStatus.OK:
            content = response.output.choices[0].message.content
            if isinstance(content, list):
                real_answer = content[0].get('text', '')
            else:
                real_answer = content

            return JsonResponse({
                "code": 200,
                "msg": "success",
                "data": {"answer": real_answer}
            })
        else:
            return JsonResponse({"code": 500, "msg": f"AI 调用失败: {response.message}", "data": None}, status=500)

    except Exception as e:
        return JsonResponse({"code": 400, "msg": f"系统异常: {str(e)}", "data": None}, status=400)


# ----------------------
# 地理搜索（高德 POI 搜索，国内网络原生支持）
# ----------------------
AMAP_KEY = "7cac8b19c3d2009ed9042836f2ca4ead"

def geo_search(request):
    q = request.GET.get('q', '').strip()
    if len(q) < 1:
        return JsonResponse({"code": 400, "msg": "请输入搜索关键词", "data": []})

    if not AMAP_KEY:
        return JsonResponse({"code": 500, "msg": "请先在 views.py 中配置 AMAP_KEY（免费获取: https://lbs.amap.com/）", "data": []})

    try:
        proxies = {"http": None, "https": None}
        url = f"https://restapi.amap.com/v3/place/text?keywords={q}&key={AMAP_KEY}&offset=8&extensions=base"
        resp = requests.get(url, timeout=8, proxies=proxies)
        data = resp.json()
        results = []
        for poi in data.get("pois", []):
            loc = poi.get("location", "0,0").split(",")
            results.append({
                "name": poi.get("name", ""),
                "display_name": f"{poi.get('pname', '')}{poi.get('cityname', '')}{poi.get('adname', '')}{poi.get('address', '')}",
                "lat": float(loc[1]) if len(loc) == 2 else 0,
                "lon": float(loc[0]) if len(loc) == 2 else 0,
                "type": poi.get("typecode", ""),
            })
        return JsonResponse({"code": 200, "data": results})
    except Exception as e:
        return JsonResponse({"code": 500, "msg": str(e), "data": []})