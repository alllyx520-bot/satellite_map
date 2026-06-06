from .base import ImageryMetadata, ImageryProvider


MAPBOX_LIMITATIONS = (
    "Mapbox Satellite Basemap 是多源融合可视化底图，不提供单张影像的明确拍摄时间、"
    "传感器、云量、原始产品编号和可复核处理级别；适合作为地图底图和粗略目视参考，"
    "不应作为单独行政决策或执法证据。"
)


class MapboxProvider(ImageryProvider):
    source = "mapbox"

    def metadata_for_bbox(self, bbox):
        return ImageryMetadata(
            source=self.source,
            source_label="Mapbox Satellite Basemap",
            processing_level="basemap",
            license_type="mapbox_terms",
            decision_grade="reference",
            limitations=MAPBOX_LIMITATIONS,
            metadata={
                "bbox": bbox,
                "traceability": "low",
                "acquisition_time_known": False,
                "cloud_percent_known": False,
            },
        )
