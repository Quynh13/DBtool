from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dbtool", "0003_tableprofile_is_released")]

    operations = [
        migrations.AddField(
            model_name="lookupmapping",
            name="output_mappings",
            field=models.JSONField(default=list, blank=True),
        ),
    ]
