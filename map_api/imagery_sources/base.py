from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImageryMetadata:
    source: str
    source_label: str
    processing_level: str
    license_type: str
    decision_grade: str
    limitations: str
    product_id: str = ""
    acquired_at: object = None
    published_at: object = None
    cloud_percent: float = None
    metadata: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "source": self.source,
            "source_label": self.source_label,
            "product_id": self.product_id,
            "acquired_at": self.acquired_at,
            "published_at": self.published_at,
            "cloud_percent": self.cloud_percent,
            "processing_level": self.processing_level,
            "license_type": self.license_type,
            "decision_grade": self.decision_grade,
            "limitations": self.limitations,
            "metadata": self.metadata,
        }


class ImageryProvider:
    source = ""

    def metadata_for_bbox(self, bbox):
        raise NotImplementedError
