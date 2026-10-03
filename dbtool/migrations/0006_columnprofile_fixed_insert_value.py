from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dbtool", "0005_configurationevent")]

    operations = [
        migrations.AddField(
            model_name="columnprofile",
            name="fixed_insert_value",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AlterField(
            model_name="columnprofile",
            name="input_mode",
            field=models.CharField(
                choices=[
                    ("text", "Direct input"),
                    ("lookup", "Lookup"),
                    ("fixed", "Fixed on insert"),
                ],
                default="text",
                max_length=20,
            ),
        ),
    ]
