# SatelliteSense 数据链审计

审计范围：Mapbox 静态底图、Sentinel-2 L2A/STAC、TiTiler 渲染、影像预处理、NDWI、VL/Agent 以及结果可信度标注。

## 结论

当前系统的主要问题不是算法数量不足，而是没有以数据源能力建立统一的数据契约。系统同时维护了“视觉截图链”和“物理波段链”，但两条链在场景、分辨率、投影、掩膜、时相和可测量性方面没有被严格区分。因此，部分输出虽然有专业术语和质量分数，实际证据等级仍不足以支撑对应结论。

## P0：会直接产生错误或误导性结果

### 1. Sentinel SCL 云掩膜很可能被错误缩放

位置：`map_api/utils/agent_tools.py:490-595`。

`fetch_cog_bbox_array()` 对所有 COG 请求都追加 `rescale=0,10000`。该参数对反射率波段可以作为显示/数值缩放的一部分，但 SCL 是离散类别栅格，类别值应保持 3、8、9、10、11 等整数。当前代码随后用 `np.rint(scl)` 与这些类别比较；如果 TiTiler 对 SCL 也执行 rescale，类别值会被压缩到接近 0，云、云影、卷云和雪将无法被正确排除。

在线实测（2026-09-07）已复现：对同一 `SCL.tif` 请求，带 `rescale=0,10000` 返回值只有 `0/255`；去掉 `rescale` 后返回值包含正确的离散类别（如 2、3、4、5、6、7、8、9、255）。

影响：NDWI 可能把云和阴影计入水体；结果会表现为“有 SCL 质量控制”，但实际掩膜失效。

整改：按资产类型分离读取契约。反射率波段读取原始数值并应用资产元数据 scale/offset；SCL 使用无 rescale 请求，强制 nearest 重采样并按整数类别掩膜；同时记录 `mask_source`、`masked_class_counts` 和掩膜后有效像元比例。

### 2. 多景拼接不是物理一致的时相合成

位置：`map_api/sentinel_pipeline.py:82-145, 405-485, 698-744`。

`compose_sentinel_mosaic()` 对多景 TCI JPEG 做“先到先填”的像素拼接。候选可以来自同日，也可以来自不同日期；场景元数据还把多景日期压缩成最大日期，并把云量取平均。

影响：拼接边界两侧可能代表不同日期、太阳高度、气溶胶和色彩条件，却被当作同一张“近期影像”交给 VL。对变化、趋势、面积比较和城市扩张的回答会产生时相混淆。平均云量也不能代表每个像元的云污染程度。

整改：默认只允许同一日期/同一轨道的覆盖拼接；跨日期只能进入明确的“覆盖补全”模式，禁止用于变化结论，并在 API/报告中逐块显示日期。真正的变化分析必须采用两期独立场景、同投影同分辨率、共同有效掩膜和指数差分。

### 3. Mapbox 的 `gsd_m` 是截图采样尺度，不是传感器 GSD

位置：`map_api/orchestrator.py:513-560`、`map_api/geo_math.py:20-47`、`map_api/payloads.py:108-125`。

Mapbox 静态 API 返回的是经过供应商融合、重采样和裁切的可视化底图。系统根据请求 bbox 和输出像素数计算 `gsd_m`，再把它传给主动感知测量和目标尺寸/面积计算。

影响：用户会看到“米/像素”并误以为这是原始影像空间分辨率。对 Mapbox 图像，数值最多表示当前导出图的地理采样间隔；它不保证真实可辨识细节，也不解决未知拍摄时间问题。基于它的建筑面积、车辆尺寸和数量置信度容易被过度表达。

整改：把字段拆成 `pixel_ground_spacing_m`（当前导出栅格采样间隔）和 `source_gsd_m`（未知/供应商未提供）。Mapbox 场景禁止进入“物理测量”路径；只允许返回像素/地图坐标近似，并明确标注“不可作为传感器 GSD 测量”。

### 4. 行政区 bbox 仍是主要影像检索范围，polygon 只在部分 NDWI 路径生效

位置：`map_api/agent/tools.py:180-220, 537-595`、`map_api/orchestrator.py:300-510`。

检索和 TCI 渲染大多使用行政区 bbox。polygon 栅格掩膜只在 NDWI 计算时附加，视觉 VL 仍可看到 bbox 内行政区外的区域；部分场景还在裁边、拼接后再做近似 polygon 裁切。

影响：区域级视觉结论、面积占比和“全区”表述可能包含行政区外像元。bbox 很凹或海岸线复杂时，误差不是边缘噪声，而是系统性偏差。

整改：建立统一 AOI 对象：原始 polygon、检索 bbox、栅格化 mask、有效像元统计都必须随 scene 保存。所有面积/比例指标必须使用 AOI mask；VL 输入至少提供 polygon 外遮罩后的图像或明确的 bbox 外部区域。

## P1：算法与数据能力不匹配

### 5. “遥感指标目录”目前不是完整的可用分析能力

位置：`map_api/remote_sensing_indices.py`、`map_api/views.py:450`、`static/browser.js:2359`。

目录声明了 NDVI、NDWI、MNDWI、NDBI、BSI、NDSI 和变化摘要，但 API 只提供目录，不提供从真实 Sentinel 资产读取对应波段、统一掩膜、重采样、阈值、输出栅格和结果落库的通用执行链。实际 Agent 只接通了 NDWI。

影响：界面展示“可用指标”会让用户误以为这些指标已经针对当前数据源可计算。尤其 Sentinel 的 SWIR 为 20m，需要与 10m 波段重采样；没有明确策略时，MNDWI/NDBI/BSI 的结果不可直接与 TCI 或 10m NDWI 等价。

整改：目录改为“能力目录”，每个指标记录所需资产、原生分辨率、重采样策略、掩膜要求和当前是否已实现。未实现的指标不要在工作台显示为可执行。

### 6. 固定阈值 NDWI 被当作跨地区、跨季节的水体比例

位置：`map_api/agent/tools.py:508-535, 608-650`。

NDWI 使用固定 `0.1` 阈值，并将有效像元中超过阈值的比例汇总为水体比例。没有按场景自适应阈值、训练区、Otsu 稳定性、混合像元或水体/阴影混淆评估。

影响：阈值对浑浊水、建筑阴影、山地阴影、稻田湿土和不同大气条件敏感。输出的百分比适合线索筛查，不能被解释为水体制图面积。

整改：至少同时输出固定阈值和 Otsu/分位数敏感性范围；如果两种阈值差异大，降级为“不可稳定量化”。将 `water_percent` 改名为 `thresholded_water_pixel_ratio`，面积输出必须带 AOI 和像元有效率。

### 7. `rescale=0,10000` 与 Sentinel 资产元数据没有形成显式反射率契约

位置：`map_api/imagery_sources/earth_search.py:260-335`、`map_api/agent/tools.py:490-507`。

STAC 资产包含 `raster:bands.scale=0.0001` 和 `offset=-0.1`。当前代码依赖 TiTiler 的 `rescale` 行为，却没有验证返回 TIFF 的数值范围、dtype、nodata 和 scale/offset 是否已经应用。

影响：不同资产（反射率、TCI、SCL）可能被同一套读取逻辑处理；一旦 TiTiler 默认行为或参数解释变化，指数会悄悄偏移而不报错。

整改：读取后记录 dtype、min/max、nodata、scale/offset；对绿色、红色、NIR、SWIR 使用统一的反射率归一化函数，并用合成像元单元测试验证 NDVI/NDWI 的已知值。

### 8. RemoteCLIP 和颜色启发式筛块并不等于“遥感目标定位”

位置：`map_api/utils/clip_retriever.py`、`map_api/utils/smart_query_analyzer.py:300-390`。

RemoteCLIP 是整块图文相似度排序；缺少模型时退化到颜色、边缘和简单 RGB 规则。它没有目标检测、语义分割或地理配准能力，且切块重叠/缩放会改变目标可见尺度。

影响：水体、植被、建筑等大类可以作为召回线索，但不能支撑“找出所有目标”“按类别计数”或精确定位。当前术语“语义分块检索”容易被误解为已完成目标级检索。

整改：降级命名为 `tile_retrieval_hint`；只把它作为 VL 输入排序，不把排序结果作为发现/遗漏证明。计数和目标定位需要专门检测/分割模型或明确的人工复核流程。

## P2：可信度和产品表达问题

### 9. 主动感知放大不能创造 Sentinel 细节

位置：`map_api/utils/active_perception.py`、`map_api/views.py:1297-1605`。

放大只是对 10m 级 Sentinel 栅格重新裁剪/插值；它可以改善模型查看布局，不能恢复建筑、车辆或屋顶材质信息。当前策略对明显细节词会关闭主动感知，但用户仍可在混合问题或直接 API 参数中触发放大。

整改：主动感知前增加数据源能力门禁；Sentinel 只允许宏观区域定位，不允许输出细节目标尺寸/数量；Mapbox 也只能输出视觉近似，不进入物理测量。

### 10. Agent 的源选择是规则优先，缺少“数据可用性”闭环

位置：`map_api/utils/analysis_strategy.py`、`map_api/utils/smart_query_analyzer.py`、`map_api/agent/loop.py`。

任务词决定 Sentinel/Mapbox，但没有在选择前统一评估 AOI 面积、目标最小尺寸、云/阴影覆盖、所需时相数量、波段是否可用和输出是否需要定量。失败后可降级到 Mapbox，但“完成了任务”与“完成了该任务所需的证据”仍未严格分开。

整改：先生成 `data_requirements`，再对每个候选源做能力匹配；若要求变化/指数/定量而只有 Mapbox，任务应返回“无法满足证据要求”，而不是仅换源继续。

## 当前真正可被可靠承诺的能力

- Mapbox：在明确写出时相未知、不是传感器 GSD、只做视觉参考的前提下，可做空间格局、道路/建筑形态的初步目视解译。
- Sentinel-2 TCI：在记录日期、云量、覆盖和有效像元率的前提下，可做区域级地类、水体/植被空间线索和近期筛查。
- Sentinel-2 NDWI：修复 SCL 读取后，可做带有效像元质量说明的水体线索比例；仍不应称为正式水体制图。
- 多时相变化：当前没有完整可信链，不能把单景、多景覆盖拼接或 VL 视觉差异称为变化检测。

## 推荐重构顺序

1. 先建立 `SceneDataContract`：source、aoi、acquisition、native_resolution、pixel_spacing、assets、mask、projection、validity、allowed_operations。
2. 修复 Sentinel 波段/SCL 读取和 QA 统计，先让 NDWI 结果可信。
3. 将 TCI 视觉链与物理波段分析链分开；Mapbox 禁止物理测量语义。
4. 关闭或隐藏未接通的指标目录执行入口，补齐逐指标资产与重采样策略后再开放。
5. 重做多景逻辑：同日覆盖拼接与跨时相变化分析分成两个产品。
6. 最后再优化 RemoteCLIP、主动感知和 Agent 计划；这些属于输入选择和交互优化，不能替代数据质量链。
