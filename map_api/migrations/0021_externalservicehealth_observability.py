from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("map_api", "0020_evidence")]
    operations = [
        migrations.AddField("externalservicehealth", "last_error_type", models.CharField(max_length=40, blank=True, default="")),
        migrations.AddField("externalservicehealth", "endpoint", models.CharField(max_length=500, blank=True, default="")),
        migrations.AddField("externalservicehealth", "last_success_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("externalservicehealth", "last_failure_at", models.DateTimeField(null=True, blank=True)),
        migrations.AddField("externalservicehealth", "latency_ms", models.PositiveIntegerField(null=True, blank=True)),
        migrations.AddField("externalservicehealth", "last_http_status", models.PositiveIntegerField(null=True, blank=True)),
        migrations.AddField("externalservicehealth", "last_retry_count", models.PositiveIntegerField(default=0)),
    ]
