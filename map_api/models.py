from django.db import models


class ChatHistory(models.Model):
    image_file = models.CharField(max_length=255, unique=True)
    messages = models.JSONField(default=list)
    spatial_context = models.CharField(max_length=500, blank=True, default="")
    bbox = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]


class DownloadTask(models.Model):
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
