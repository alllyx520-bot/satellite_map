from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("map_api", "0010_agentsession_message_request_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="message_request_states",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
