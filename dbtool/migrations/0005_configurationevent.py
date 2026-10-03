from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dbtool", "0004_lookupmapping_output_mappings"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfigurationEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("schema_name", models.CharField(max_length=63)),
                ("table_name", models.CharField(max_length=63)),
                ("action", models.CharField(choices=[("CONFIG_CHANGED", "Configuration changed"), ("RELEASE", "Released"), ("UNRELEASE", "Unreleased"), ("AUTO_UNRELEASE", "Automatically unreleased")], max_length=24)),
                ("was_released", models.BooleanField(default=False)),
                ("is_released", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
