from django.db import models


class ChatHistory(models.Model):
    image_file = models.CharField(max_length=255)
    messages = models.JSONField(default=list)
    spatial_context = models.CharField(max_length=500, blank=True, default="")
    bbox = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
