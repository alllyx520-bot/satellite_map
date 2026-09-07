from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("map_api", "0008_agentsession_request_id")]

    operations = [
        migrations.CreateModel(
            name="ExternalServiceHealth",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("service_key", models.CharField(max_length=120, unique=True)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("open_until", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["service_key"]},
        ),
    ]
