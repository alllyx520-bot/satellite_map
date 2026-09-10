from .base import ImageryMetadata, ImageryProvider


TIANDITU_LIMITATIONS = (
    "天地图影像是国家地理信息公共服务平台的可视化底图，不提供单张影像的明确拍摄时间、"
    "传感器、云量和可复核处理级别；仅作为视觉参考，不应作为行政决策或执法证据；"
    "使用时需保留天地图版权标注。"
)


class TiandituProvider(ImageryProvider):
    source = "tianditu"

    def metadata_for_bbox(self, bbox):
        return ImageryMetadata(
            source=self.source,
            source_label="天地图影像",
            processing_level="basemap",
            license_type="tianditu_terms",
            decision_grade="reference",
            limitations=TIANDITU_LIMITATIONS,
            metadata={
                "bbox": bbox,
                "traceability": "low",
                "acquisition_time_known": False,
                "cloud_percent_known": False,
            },
        )
