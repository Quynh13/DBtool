from django.db import migrations, models
import django.db.models.deletion


def link_current_lifetimes(apps, schema_editor):
    OperationLog = apps.get_model("audit", "OperationLog")
    ToolOwnedRecord = apps.get_model("audit", "ToolOwnedRecord")
    for owned in ToolOwnedRecord.objects.select_related("origin").iterator():
        OperationLog.objects.filter(
            schema_name=owned.schema_name,
            table_name=owned.table_name,
            record_key=owned.record_key,
            created_at__gte=owned.origin.created_at,
        ).update(record_origin_id=owned.origin_id)


class Migration(migrations.Migration):
    dependencies = [("audit", "0004_toolownedrecord_last_operation")]

    operations = [
        migrations.AlterField(
            model_name="operationlog", name="action",
            field=models.CharField(
                choices=[("INSERT", "Insert"), ("UPDATE", "Update"),
                         ("DELETE", "Delete"), ("RESTORE", "Restore")],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="operationlog", name="record_origin",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="record_history", to="audit.operationlog",
            ),
        ),
        migrations.RunPython(link_current_lifetimes, migrations.RunPython.noop),
    ]
