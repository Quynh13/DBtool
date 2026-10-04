from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dbtool", "0006_columnprofile_fixed_insert_value")]

    operations = [
        migrations.AddField(
            model_name="configurationevent", name="visible_fields",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="configurationevent", name="added_fields",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="configurationevent", name="removed_fields",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
