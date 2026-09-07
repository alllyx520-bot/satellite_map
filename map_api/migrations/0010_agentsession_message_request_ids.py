from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("map_api", "0009_externalservicehealth")]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="message_request_ids",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
