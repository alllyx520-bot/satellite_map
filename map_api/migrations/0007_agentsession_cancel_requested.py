from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("map_api", "0006_agentsession")]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="cancel_requested",
            field=models.BooleanField(default=False),
        ),
    ]
