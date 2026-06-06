from django.db import models


class ChatHistory(models.Model):
    scene = models.ForeignKey("ImageryScene", null=True, blank=True, on_delete=models.SET_NULL, related_name="chat_histories")
    image_file = models.CharField(max_length=255, unique=True)
    messages = models.JSONField(default=list)
    spatial_context = models.CharField(max_length=500, blank=True, default="")
    bbox = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class DownloadTask(models.Model):
    scene = models.ForeignKey("ImageryScene", null=True, blank=True, on_delete=models.SET_NULL, related_name="download_tasks")
    file_name = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, default="downloading")
    total = models.PositiveIntegerField(default=1)
    done = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    min_lng = models.FloatField()
    min_lat = models.FloatField()
    max_lng = models.FloatField()
    max_lat = models.FloatField()
    gsd_m = models.FloatField(default=0)
    area_km2 = models.FloatField(default=0)
    resolution_px = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class ImageryScene(models.Model):
    SOURCE_MAPBOX = "mapbox"
    GRADE_REFERENCE = "reference"
    GRADE_SCREENING = "screening"
    GRADE_DECISION_SUPPORT = "decision_support"
    GRADE_EVIDENCE = "evidence"

    file_name = models.CharField(max_length=255, unique=True)
    source = models.CharField(max_length=50, default=SOURCE_MAPBOX)
    source_label = models.CharField(max_length=120, default="Mapbox Satellite Basemap")
    product_id = models.CharField(max_length=255, blank=True, default="")
    acquired_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)
    min_lng = models.FloatField()
    min_lat = models.FloatField()
    max_lng = models.FloatField()
    max_lat = models.FloatField()
    gsd_m = models.FloatField(default=0)
    area_km2 = models.FloatField(default=0)
    cloud_percent = models.FloatField(null=True, blank=True)
    processing_level = models.CharField(max_length=80, default="basemap")
    license_type = models.CharField(max_length=80, default="mapbox_terms")
    decision_grade = models.CharField(max_length=50, default=GRADE_REFERENCE)
    limitations = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
