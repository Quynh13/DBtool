from django.db import models


class TableProfile(models.Model):
    schema_name = models.CharField(max_length=63)
    table_name = models.CharField(max_length=63)
    display_label = models.CharField(max_length=200, blank=True)
    mutation_enabled = models.BooleanField(
        default=False,
        help_text="Explicitly allow mutations once every column is configured.",
    )

    class Meta:
        ordering = ("schema_name", "table_name")
        constraints = [
            models.UniqueConstraint(
                fields=("schema_name", "table_name"),
                name="dbtool_unique_table_profile",
            )
        ]

    def __str__(self):
        return f"{self.schema_name}.{self.table_name}"


class ColumnProfile(models.Model):
    class InputMode(models.TextChoices):
        TEXT = "text", "Direct input"
        LOOKUP = "lookup", "Lookup"

    table_profile = models.ForeignKey(
        TableProfile,
        on_delete=models.CASCADE,
        related_name="column_profiles",
    )
    column_name = models.CharField(max_length=63)
    display_label = models.CharField(max_length=200, blank=True)
    show_in_form = models.BooleanField(default=True)
    editable = models.BooleanField(default=True)
    required_on_insert = models.BooleanField(default=False)
    allow_clear_on_update = models.BooleanField(default=True)
    input_mode = models.CharField(
        max_length=20,
        choices=InputMode.choices,
        default=InputMode.TEXT,
    )
    help_text = models.TextField(blank=True)
    reviewed = models.BooleanField(
        default=False,
        help_text="Confirms that the semantic settings were reviewed by a user.",
    )

    class Meta:
        ordering = ("table_profile", "column_name")
        constraints = [
            models.UniqueConstraint(
                fields=("table_profile", "column_name"),
                name="dbtool_unique_column_profile",
            )
        ]

    def __str__(self):
        return f"{self.table_profile}.{self.column_name}"


class LookupMapping(models.Model):
    column_profile = models.OneToOneField(
        ColumnProfile,
        on_delete=models.CASCADE,
        related_name="lookup_mapping",
    )
    target_schema = models.CharField(max_length=63)
    target_table = models.CharField(max_length=63)
    target_value_column = models.CharField(max_length=63)
    display_columns = models.JSONField(default=list)
    search_columns = models.JSONField(default=list)
    allow_null = models.BooleanField(default=False)
    allow_arbitrary_value = models.BooleanField(default=False)

    def __str__(self):
        return (
            f"{self.column_profile} -> "
            f"{self.target_schema}.{self.target_table}.{self.target_value_column}"
        )
