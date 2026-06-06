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


@dataclass(frozen=True)
class ImageryCandidate:
    source: str
    source_label: str
    collection: str
    item_id: str
    product_id: str
    acquired_at: object
    published_at: object
    bbox: dict
    gsd_m: float
    cloud_percent: float
    processing_level: str
    license_type: str
    decision_grade: str
    suitability_score: int
    score_reasons: list
    limitations: str
    assets: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def as_dict(self):
        return {
            "source": self.source,
            "source_label": self.source_label,
            "collection": self.collection,
            "item_id": self.item_id,
            "product_id": self.product_id,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "bbox": self.bbox,
            "gsd_m": self.gsd_m,
            "cloud_percent": self.cloud_percent,
            "processing_level": self.processing_level,
            "license_type": self.license_type,
            "decision_grade": self.decision_grade,
            "suitability_score": self.suitability_score,
            "score_reasons": self.score_reasons,
            "limitations": self.limitations,
            "assets": self.assets,
            "links": self.links,
            "metadata": self.metadata,
        }


class ImageryProvider:
    source = ""

    def metadata_for_bbox(self, bbox):
        raise NotImplementedError
