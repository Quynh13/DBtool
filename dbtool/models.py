from django.db import models
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver


class TableProfile(models.Model):
    schema_name = models.CharField(max_length=63)
    table_name = models.CharField(max_length=63)
    display_label = models.CharField(max_length=200, blank=True)
    mutation_enabled = models.BooleanField(
        default=False,
        help_text="Explicitly allow mutations once every column is configured.",
    )
    is_released = models.BooleanField(default=False)

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


TABLE_SEMANTIC_FIELDS = (
    "schema_name",
    "table_name",
    "display_label",
    "mutation_enabled",
)
COLUMN_SEMANTIC_FIELDS = (
    "table_profile_id",
    "column_name",
    "display_label",
    "show_in_form",
    "editable",
    "required_on_insert",
    "allow_clear_on_update",
    "input_mode",
    "help_text",
    "reviewed",
)
LOOKUP_SEMANTIC_FIELDS = (
    "column_profile_id",
    "target_schema",
    "target_table",
    "target_value_column",
    "display_columns",
    "search_columns",
    "allow_null",
    "allow_arbitrary_value",
)


def _persisted_values(sender, instance, fields):
    if instance._state.adding:
        return None
    return sender._base_manager.filter(pk=instance.pk).values(*fields).first()


def _has_semantic_change(previous, instance, fields):
    return previous is None or any(
        previous[field] != getattr(instance, field) for field in fields
    )


def _unrelease_profiles(profile_ids):
    profile_ids = {profile_id for profile_id in profile_ids if profile_id}
    if profile_ids:
        TableProfile.objects.filter(
            pk__in=profile_ids,
            is_released=True,
        ).update(is_released=False)


def _profile_ids_for_columns(column_profile_ids):
    return set(
        ColumnProfile.objects.filter(
            pk__in={value for value in column_profile_ids if value}
        ).values_list("table_profile_id", flat=True)
    )


SEMANTIC_FIELDS = {
    TableProfile: TABLE_SEMANTIC_FIELDS,
    ColumnProfile: COLUMN_SEMANTIC_FIELDS,
    LookupMapping: LOOKUP_SEMANTIC_FIELDS,
}


@receiver(pre_save, sender=TableProfile)
@receiver(pre_save, sender=ColumnProfile)
@receiver(pre_save, sender=LookupMapping)
def _track_semantic_change(sender, instance, **_kwargs):
    fields = SEMANTIC_FIELDS[sender]
    previous = _persisted_values(sender, instance, fields)
    instance._semantic_changed = (
        previous is not None or sender is not TableProfile
    ) and _has_semantic_change(previous, instance, fields)

    if sender is TableProfile:
        instance._semantic_profile_ids = {instance.pk}
    elif sender is ColumnProfile:
        instance._semantic_profile_ids = {
            instance.table_profile_id,
            previous["table_profile_id"] if previous else None,
        }
    else:
        instance._semantic_profile_ids = _profile_ids_for_columns(
            {
                instance.column_profile_id,
                previous["column_profile_id"] if previous else None,
            }
        )


@receiver(post_save, sender=TableProfile)
@receiver(post_save, sender=ColumnProfile)
@receiver(post_save, sender=LookupMapping)
def _unrelease_after_semantic_change(sender, instance, **_kwargs):
    if getattr(instance, "_semantic_changed", False):
        _unrelease_profiles(instance._semantic_profile_ids)
        if sender is TableProfile:
            instance.is_released = False


@receiver(post_delete, sender=ColumnProfile)
@receiver(post_delete, sender=LookupMapping)
def _unrelease_after_semantic_delete(sender, instance, **_kwargs):
    profile_ids = (
        {instance.table_profile_id}
        if sender is ColumnProfile
        else _profile_ids_for_columns({instance.column_profile_id})
    )
    _unrelease_profiles(profile_ids)
