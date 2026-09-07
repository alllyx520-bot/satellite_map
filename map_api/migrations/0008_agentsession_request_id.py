from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("map_api", "0007_agentsession_cancel_requested")]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="request_id",
            field=models.CharField(blank=True, max_length=120, null=True, unique=True),
        ),
    ]
