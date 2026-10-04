from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0005_operationlog_record_origin"),
        ("dbtool", "0007_configurationevent_form_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationlog", name="form_release",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="record_operations", to="dbtool.configurationevent",
            ),
        ),
    ]
