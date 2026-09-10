from .base import ImageryMetadata, ImageryProvider


ESRI_LIMITATIONS = (
    "Esri World Imagery 是多源融合可视化底图，不提供单张影像的明确拍摄时间、"
    "传感器、云量和可复核处理级别；免费条款限非营运用途并需署名 Esri 及数据提供方；"
    "仅作为视觉参考，不应作为行政决策或执法证据。"
)


class EsriProvider(ImageryProvider):
    source = "esri"

    def metadata_for_bbox(self, bbox):
        return ImageryMetadata(
            source=self.source,
            source_label="Esri World Imagery",
            processing_level="basemap",
            license_type="esri_terms",
            decision_grade="reference",
            limitations=ESRI_LIMITATIONS,
            metadata={
                "bbox": bbox,
                "traceability": "low",
                "acquisition_time_known": False,
                "cloud_percent_known": False,
            },
        )
