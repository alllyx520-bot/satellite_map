import re
import json
import os
from PIL import Image


# ── ZoomEarth-inspired Two-Stage Active Perception Prompt ──────────────
# Directly adapted from ZoomEarth demo.py INSTRUCTION template.
# Model decides: (a) where to zoom, (b) whether zoom is even needed.

STAGE1_SYSTEM = """你是 SatelliteSense，顶级遥感图像分析专家。请对提供的卫星影像进行**结构化主动感知分析**。

## 分析流程
请在 <think></think> 标签内完成第一步推理，然后决定是否需要局部放大。

### 步骤 1 (think)
1. 用一句话客观描述影像的整体场景。
2. 判断问题类型：
   - **全局问题**（用地类型、植被覆盖度、城市形态等宏观分析）→ 无需放大，直接回答
   - **局部细节问题**（具体建筑特征、小目标识别、文字判读等微观分析）→ 需要先定位关键区域再放大
3. 如果需要放大：输出一个 JSON 格式的定位框
   ```json
   [{"bbox_2d": [x_min, y_min, x_max, y_max], "label": "区域简述"}]
   ```
   坐标为影像上的绝对像素坐标（整数），左上角为原点。
   如果不确定具体位置，请给出最佳估计——**不要说"不确定"**。
4. 如果不需要放大：直接跳到步骤 3 输出 <answer>。

### 步骤 2 (think) — 仅当步骤1决定放大时
- 描述放大后看到的关键细节
- 逐步推理至最终答案

### 步骤 3 (answer)
- 在 <answer></answer> 标签内给出最终分析结果"""


ZOOOMEARTH_INSTRUCTION = """请按照以下格式输出：

<think>
{场景描述 + 问题意图判定 + 定位策略}
{如果需放大：输出 bbox JSON；如果不需要："无需放大，直接分析如下"}
</think>

{如果需放大，此处为看放大区域后的推理：
<think>放大后的细节推理</think>
}

<answer>
{最终分析结果——专业、客观、用数据说话}
</answer>"""


def build_stage1_prompt(question, spatial_ctx=""):
    prompt = STAGE1_SYSTEM + "\n\n"
    if spatial_ctx:
        prompt += f"## 空间上下文\n{spatial_ctx}\n\n"
    prompt += f"## 当前问题\n{question}\n\n"
    prompt += ZOOOMEARTH_INSTRUCTION
    return prompt


# ── BBox Parsing (from ZoomEarth extract_bbox) ─────────────────────────

def extract_bbox_from_response(response_text, scale_factor=1.0):
    if not response_text:
        return []

    patterns = [
        r'"bbox_2d"\s*[:=]\s*\[([^\]]+)\]',
        r'bbox_2d["\s:]*\[([^\]]+)\]',
        r'\[(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\]',
    ]

    for pattern in patterns:
        match = re.search(pattern, response_text)
        if match:
            groups = match.groups()
            if len(groups) == 1:
                coords = [int(x.strip()) for x in groups[0].split(',') if x.strip().isdigit()]
            else:
                coords = [int(x) for x in groups if x.isdigit() or (x.lstrip('-').isdigit())]

            if len(coords) >= 4:
                bbox = coords[:4]
                if scale_factor != 1.0:
                    bbox = [int(c * scale_factor) for c in bbox]
                return [bbox]

    try:
        think_match = re.search(r'<think>(.*?)</think>', response_text, re.DOTALL)
        if think_match:
            think_text = think_match.group(1)
            json_match = re.search(r'\[\s*\{[^}]+\}\s*\]', think_text, re.DOTALL)
            if json_match:
                obj = json.loads(json_match.group(0))
                if isinstance(obj, list) and len(obj) > 0:
                    if 'bbox_2d' in obj[0]:
                        bbox = obj[0]['bbox_2d']
                        if scale_factor != 1.0:
                            bbox = [int(c * scale_factor) for c in bbox]
                        return [bbox]
    except (json.JSONDecodeError, KeyError, ValueError):
        pass

    return []


# ── Crop + Resize (from ZoomEarth cut_image + resize_image) ────────────

def cut_image_geom(image_path, bbox, min_size=512, max_size=3584):
    """裁剪 + 必要时缩小。返回 (crop_path, orig_box, saved_w, saved_h):
    - orig_box=(x1,y1,x2,y2):实际裁剪框在【输入图】中的坐标(经 clamp/min_size 调整后)。
    - saved_w/saved_h:落盘裁剪图尺寸(可能因 max_size 被缩小)。
    多级放大时,需用这组几何把"模型在裁剪图上给的 bbox"映射回输入图坐标。
    失败返回 (None, None, 0, 0)。"""
    if not os.path.exists(image_path):
        return None, None, 0, 0

    img = Image.open(image_path)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    w, h = img.size

    x1, y1, x2, y2 = bbox
    x1 = max(0, min(int(x1), w - 1))
    y1 = max(0, min(int(y1), h - 1))
    x2 = max(x1 + 1, min(int(x2), w))
    y2 = max(y1 + 1, min(int(y2), h))

    crop_w = x2 - x1
    crop_h = y2 - y1
    if crop_w < min_size or crop_h < min_size:
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        half = min_size // 2
        x1 = max(0, cx - half)
        y1 = max(0, cy - half)
        x2 = min(w, x1 + min_size)
        y2 = min(h, y1 + min_size)
        x1 = max(0, x2 - min_size)
        y1 = max(0, y2 - min_size)

    cropped = img.crop((x1, y1, x2, y2))

    cr_w, cr_h = cropped.size
    if max(cr_w, cr_h) > max_size:
        ratio = max_size / max(cr_w, cr_h)
        cropped = cropped.resize((int(cr_w * ratio), int(cr_h * ratio)), Image.LANCZOS)

    saved_w, saved_h = cropped.size
    base, ext = os.path.splitext(image_path)
    crop_path = f"{base}_crop_{x1}_{y1}_{x2}_{y2}.jpg"
    cropped.save(crop_path, 'JPEG', quality=92)
    return crop_path, (x1, y1, x2, y2), saved_w, saved_h


def cut_image(image_path, bbox, min_size=512, max_size=3584):
    """向后兼容:只要裁剪图路径。"""
    crop_path, _, _, _ = cut_image_geom(image_path, bbox, min_size, max_size)
    return crop_path


def map_bbox_to_original(view_bbox, orig_box, saved_w, saved_h):
    """把模型在裁剪图(saved_w×saved_h)上给的 view_bbox,映射回输入图坐标。
    orig_box 是该裁剪框在输入图中的范围 (x1,y1,x2,y2)。用于多级迭代放大的坐标回溯。"""
    ox1, oy1, ox2, oy2 = orig_box
    sx = (ox2 - ox1) / saved_w if saved_w else 1.0
    sy = (oy2 - oy1) / saved_h if saved_h else 1.0
    vx1, vy1, vx2, vy2 = view_bbox
    return [int(ox1 + vx1 * sx), int(oy1 + vy1 * sy),
            int(ox1 + vx2 * sx), int(oy1 + vy2 * sy)]


# ── GSD 测量 + 像素→经纬度反算(遥感内核) ─────────────────────────────

def measure_bbox(bbox, gsd):
    """像素 bbox + GSD(米/像素) → 真实尺寸。返回 (width_m, height_m, area_m2)。
    bbox 为原图像素坐标 [x1,y1,x2,y2]。"""
    x1, y1, x2, y2 = bbox
    px_w = max(0, x2 - x1)
    px_h = max(0, y2 - y1)
    width_m = px_w * gsd
    height_m = px_h * gsd
    return width_m, height_m, width_m * height_m


def pixel_bbox_to_geo(bbox, orig_w, orig_h, geo_bbox):
    """像素 bbox 中心 → 经纬度。geo_bbox 为该图的地理范围
    dict(min_lng,max_lng,min_lat,max_lat)。成功返回 (lng, lat),否则 None。"""
    if not geo_bbox or not orig_w or not orig_h:
        return None
    try:
        min_lng = float(geo_bbox["min_lng"]); max_lng = float(geo_bbox["max_lng"])
        min_lat = float(geo_bbox["min_lat"]); max_lat = float(geo_bbox["max_lat"])
    except (KeyError, TypeError, ValueError):
        return None
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    lng = min_lng + (cx / orig_w) * (max_lng - min_lng)
    lat = max_lat - (cy / orig_h) * (max_lat - min_lat)   # 图像 y 向下,纬度向上
    return lng, lat


# ── Stage 2 Prompt Builder ─────────────────────────────────────────────

def build_stage2_prompt(question, stage1_text, bbox_label=""):
    prompt = f"""你已经完成了全局分析并定位到关键区域（{bbox_label}）。
现在请在放大后的局部高清影像上，进行**精细分析**。

## 原始问题
{question}

## 要求
1. 在 <think> 中结合局部细节进行推理
2. 在 <answer> 中给出最终答案
3. 专业、客观、用遥感标准术语"""
    return prompt


# ── Resize Image (from ZoomEarth resize_image) ─────────────────────────

def resize_image(image_path, max_size=1024):
    if not os.path.exists(image_path):
        return None

    img = Image.open(image_path)
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    w, h = img.size

    if max(w, h) <= max_size:
        return image_path

    ratio = max_size / max(w, h)
    new_w = int(w * ratio)
    new_h = int(h * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    base, ext = os.path.splitext(image_path)
    resized_path = f"{base}_stage1_{max_size}.jpg"
    img.save(resized_path, 'JPEG', quality=90)
    return resized_path


# ── Has BBox check (AdaptVision-inspired) ──────────────────────────────

def model_wants_zoom(response_text):
    if not response_text:
        return False
    bboxes = extract_bbox_from_response(response_text)
    return len(bboxes) > 0


# ── Answer Extraction ──────────────────────────────────────────────────

def extract_answer_text(response_text):
    if not response_text:
        return response_text
    match = re.search(r'<answer>\s*(.*?)\s*</answer>', response_text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r'</think>\s*(.*)', response_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return response_text.strip()
