from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("map_api", "0011_agentsession_message_request_states"),
    ]

    operations = [
        migrations.AddField(
            model_name="agentsession",
            name="owner_session_key",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
    ]
