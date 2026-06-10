import re
import math
from PIL import Image
from collections import Counter


# ── Query Intent Classification ────────────────────────────────────
# Borrowed from ImageRAG's keyword extraction + AdaptVision's query-guided
# resolution decision. Since we use API (no model internals), we implement
# the decision logic in preprocessing layer via keyword + pattern rules.

INTENT_PATTERNS = {
    'detail': [
        r'(建筑|房屋|楼|厂房|住宅|屋顶|墙体|窗户)',
        r'(道路|公路|街道|高速|铁路|交叉口|立交|桥梁|隧道)',
        r'(车辆|汽车|卡车|船|飞机|集装箱|停车)',
        r'(文字|标识|标牌|车牌|号码|标签)',
        r'(密度|数量|计数|多少个|多少辆|几栋|几个)',
        r'(管线|杆塔|电塔|基站|光伏板|风机)',
        r'(裂缝|滑坡|崩塌|塌陷|沉降|变形|破损)',
        r'(颜色|色调|红色|蓝色|白色|黑色|灰色|绿色)',
        r'(材质|表面|屋顶材料|铺装)',
        r'(尺寸|大小|长宽高|面积多少)',
    ],
    'macro': [
        r'(用地类型|土地利用|土地覆盖|地类|用地构成|用地结构)',
        r'(农业|农田|耕地|田块|作物|长势|种植|高标准农田)',
        r'(城市规划|城市形态|城市格局|城镇体系|城市化)',
        r'(生态|环境|植被覆盖|绿地|森林|草原|湿地|荒漠)',
        r'(水系|流域|河流|湖泊|水库|海洋|海岸)',
        r'(地形|地貌|山地|平原|丘陵|盆地|高原)',
        r'(区域|片区|地带|全图|整体|总体|全局|宏观)',
        r'(功能分区|功能区|居住区|商业区|工业区)',
        r'(发展|趋势|变化|演变|变迁)',
    ],
    'compare': [
        r'(对比|比较|差异|区别|异同|哪个更|哪边)',
        r'(A.*B|区域.*区域|图.*图|左.*右|上.*下)',
    ],
    'table': [
        r'(表格|列表|汇总|总结|分类|分项)',
        r'(列出|罗列|列举|逐项)',
    ],
}

ENTITY_PATTERNS = {
    'water': [
        r'(水体|水域|河流|河|江|湖|海洋|海|水|水库|池塘|鱼塘|湿地|沼泽|岸线|海岸|滩涂)',
        r'(蓝色|深色|镜面|反光)',
    ],
    'vegetation': [
        r'(植被|绿化|树|森林|林地|草地|草原|农田|耕地|庄稼|作物|稻田|麦田)',
        r'(绿色|绿地|绿化带|公园|花园)',
    ],
    'agriculture': [
        r'(农业|农田|耕地|田块|田埂|农作物|作物|庄稼|稻田|麦田|玉米地|温室|大棚)',
        r'(种植|长势|撂荒|复种|高标准农田|永久基本农田)',
    ],
    'building': [
        r'(建筑|房屋|楼房|住宅|小区|别墅|高楼|大厦|厂房|仓库|棚户|城中村)',
        r'(屋顶|墙体|框架|结构|密度)',
    ],
    'road': [
        r'(道路|公路|路网|街道|交叉口|十字路口|支路|主干道|高速|铁路|轨道)',
        r'(车流|交通|拥堵|通达)',
    ],
    'vehicle': [
        r'(车辆|汽车|小车|轿车|卡车|货车|公交|停车|停车场|车位|船|船舶|飞机|集装箱)',
        r'(多少辆|数车|计数|小目标)',
    ],
    'mountain': [
        r'(山|山脉|山坡|山脊|山谷|丘陵|坡地|陡坡|悬崖|峭壁)',
    ],
    'urban': [
        r'(城市|城区|市区|城镇|街道|街区|商业区|CBD|工业园|开发区|新城)',
        r'(中心|核心区|老城|新区)',
    ],
    'infrastructure': [
        r'(机场|港口|码头|车站|火车站|枢纽|电厂|水厂|立交|桥梁|隧道)',
    ],
}

SPATIAL_HINTS = {
    'east':  r'(东|东部|东边|东侧|以东|右边|右)',
    'west':  r'(西|西部|西边|西侧|以西|左边|左)',
    'north': r'(北|北部|北边|北侧|以北|上边|上方|顶|上)',
    'south': r'(南|南部|南边|南侧|以南|下边|下方|底|下)',
    'center': r'(中心|中部|中央|中间|核心|正中)',
    'edge':   r'(边缘|边界|外围|周边|角落)',
    'along_river': r'(沿河|河边|河岸|江边|江岸|水边|湖畔|滨水|临水|海岸)',
    'along_road': r'(沿路|路边|道路沿线|沿街|街道沿线|公路沿线)',
    'near': r'(附近|旁边|周围|邻近|靠近)',
}


def analyze_query(question):
    norm = question.strip().lower()

    intent_scores = Counter()
    for intent, patterns in INTENT_PATTERNS.items():
        for p in patterns:
            if re.search(p, question):
                intent_scores[intent] += 1

    if intent_scores:
        primary_intent = intent_scores.most_common(1)[0][0]
    else:
        primary_intent = 'macro'

    is_detail = primary_intent == 'detail'
    is_macro = primary_intent == 'macro'
    is_compare = primary_intent == 'compare' or intent_scores.get('compare', 0) > 0
    needs_table = intent_scores.get('table', 0) > 0
    is_mixed = (is_detail and intent_scores.get('macro', 0) > 0) or \
               (is_macro and intent_scores.get('detail', 0) > 0)

    entities = Counter()
    for entity, patterns in ENTITY_PATTERNS.items():
        for p in patterns:
            if re.search(p, question):
                entities[entity] += 1

    spatial = {}
    for direction, pattern in SPATIAL_HINTS.items():
        match = re.search(pattern, norm)
        if match:
            spatial[direction] = True

    min_resolution = 512
    if is_detail or is_mixed:
        min_resolution = 1536
    if is_macro and not is_detail:
        min_resolution = 768

    return {
        'intent': primary_intent,
        'is_detail': is_detail,
        'is_macro': is_macro,
        'is_compare': is_compare,
        'is_mixed': is_mixed,
        'needs_table': needs_table,
        'entities': dict(entities),
        'spatial_hints': spatial,
        'min_resolution': min_resolution,
        'suggest_stages': is_detail or is_mixed,
    }


# ── Tile Relevance Scoring (ImageRAG-inspired) ──────────────────────────
# ImageRAG Fast Path: CLIP text→image similarity. We don't have CLIP locally,
# so we use simple CV features (color histogram, edge density, texture) as a
# proxy — same principle, different encoder.

def compute_tile_features(tile_path):
    img = Image.open(tile_path).convert('RGB')
    w, h = img.size
    pixels = img.load()
    if not pixels:
        return None

    total = w * h
    r_sum = g_sum = b_sum = 0
    water_count = 0
    veg_count = 0
    urban_count = 0

    sample_step = max(1, min(w, h) // 32)
    for y in range(0, h, sample_step):
        for x in range(0, w, sample_step):
            r, g, b_val = pixels[x, y]
            r_sum += r; g_sum += g; b_sum += b_val
            if b_val > r and b_val > g and b_val > 80:
                water_count += 1
            elif g > r * 0.8 and g > b_val * 0.8 and g > 60:
                veg_count += 1
            elif (r + g + b_val) / 3 > 120 and abs(r - g) < 40 and abs(r - b_val) < 40:
                urban_count += 1

    sampled = total // (sample_step * sample_step) if total // (sample_step * sample_step) > 0 else 1
    features = {
        'mean_r': r_sum / sampled,
        'mean_g': g_sum / sampled,
        'mean_b': b_sum / sampled,
        'water_ratio': water_count / sampled,
        'veg_ratio': veg_count / sampled,
        'urban_ratio': urban_count / sampled,
    }

    gray = img.convert('L')
    edge_count = 0
    edge_pixels = gray.load()
    for y in range(1, h - 1, sample_step):
        for x in range(1, w - 1, sample_step):
            dx = abs(edge_pixels[x + 1, y] - edge_pixels[x - 1, y])
            dy = abs(edge_pixels[x, y + 1] - edge_pixels[x, y - 1])
            if dx + dy > 50:
                edge_count += 1
    features['edge_density'] = edge_count / sampled

    return features


def score_tile_relevance(tile_features, query_entities, spatial_hints, tile_index, tile_cols, tile_rows):
    if not tile_features or not query_entities:
        return 0.5

    score = 0.0
    weight_sum = 0.0

    entity_feature_map = {
        'water': 'water_ratio',
        'vegetation': 'veg_ratio',
        'building': 'urban_ratio',
        'urban': 'urban_ratio',
        'road': 'edge_density',
        'infrastructure': 'edge_density',
    }

    for entity, count in query_entities.items():
        feat_key = entity_feature_map.get(entity)
        if feat_key and feat_key in tile_features:
            weight = count
            score += tile_features[feat_key] * weight
            weight_sum += weight

    if weight_sum > 0:
        score /= weight_sum

    if spatial_hints and tile_cols > 1 and tile_rows > 1:
        r = tile_index // tile_cols
        c = tile_index % tile_cols
        third_h = tile_rows / 3
        third_w = tile_cols / 3

        if spatial_hints.get('north') and r < third_h:
            score *= 1.3
        if spatial_hints.get('south') and r >= 2 * third_h:
            score *= 1.3
        if spatial_hints.get('east') and c >= 2 * third_w:
            score *= 1.3
        if spatial_hints.get('west') and c < third_w:
            score *= 1.3
        if spatial_hints.get('center') and third_h <= r < 2 * third_h and third_w <= c < 2 * third_w:
            score *= 1.3

    return min(score, 1.0)


# 中文实体 → 遥感英文语义短语(RemoteCLIP 文本编码器是英文的,中文直接编码效果差)
ENTITY_EN = {
    'water': 'water, river, lake, sea or reservoir',
    'vegetation': 'vegetation, forest, farmland or green field',
    'agriculture': 'cropland, farmland, crop field, paddy field or greenhouse',
    'building': 'buildings or residential district',
    'road': 'roads, highways or road network',
    'vehicle': 'vehicles, parking lot, ships, aircraft or small objects',
    'mountain': 'mountains, hills or rugged terrain',
    'urban': 'dense urban area, city center',
    'infrastructure': 'airport, port, station or large infrastructure',
}


def _build_clip_query(entities):
    """把抽到的实体拼成一句英文遥感检索短语。无实体返回 None。"""
    if not entities:
        return None
    ordered = sorted(entities.items(), key=lambda kv: kv[1], reverse=True)
    phrases = [ENTITY_EN[e] for e, _ in ordered if e in ENTITY_EN]
    if not phrases:
        return None
    return "a satellite remote sensing image of " + "; ".join(phrases)


def rank_tiles(tile_paths, question, tile_cols=0, tile_rows=0, top_k=4):
    analysis = analyze_query(question)
    entities = analysis['entities']
    spatial = analysis['spatial_hints']

    # —— 优先 RemoteCLIP 真检索:中文实体 → 英文语义短语 → 文本/图块余弦相似度 ——
    clip_query = _build_clip_query(entities)
    if clip_query and tile_paths:
        try:
            from . import clip_retriever
            clip_scores = clip_retriever.score_tiles(tile_paths, clip_query)
        except Exception:
            clip_scores = None
        if clip_scores:
            clip_scores.sort(key=lambda x: x[1], reverse=True)
            analysis['_ranker'] = 'remoteclip'
            return [p for p, _ in clip_scores[:top_k]], analysis

    # —— Fallback:无实体 / RemoteCLIP 不可用 → 原颜色直方图启发式 ——
    analysis['_ranker'] = 'heuristic'
    if not entities and tile_paths:
        overview_idx = 0 if 'overview' in (tile_paths[0] or '').lower() else None
        detail_tiles = tile_paths[1:] if overview_idx is not None else tile_paths
        return detail_tiles[:min(top_k, len(detail_tiles))], analysis

    scored = []
    for i, tp in enumerate(tile_paths):
        feat = compute_tile_features(tp)
        if feat:
            s = score_tile_relevance(feat, entities, spatial, i, tile_cols, tile_rows)
            scored.append((i, s, tp))

    scored.sort(key=lambda x: x[1], reverse=True)

    selected = [item[2] for item in scored[:top_k]]

    return selected, analysis


# ── Adaptive Resolution ────────────────────────────────────────────────
# Q-Zoom-inspired: adjust max_dim based on question type

def adaptive_resolution(question, base_dim):
    analysis = analyze_query(question)

    if analysis['is_detail']:
        return max(base_dim, 3584)
    elif analysis['is_macro']:
        return min(base_dim, 1536)
    return base_dim
