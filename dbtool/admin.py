from django.contrib import admin

from dbtool.models import ColumnProfile, ConfigurationEvent, LookupMapping, TableProfile


admin.site.index_template = "dbtool/admin_index.html"


class ColumnProfileInline(admin.TabularInline):
    model = ColumnProfile
    extra = 0


class RootConfigurationAdmin(admin.ModelAdmin):
    actions = None

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def _profile_state(self, obj):
        if isinstance(obj, TableProfile):
            profile = obj
        elif isinstance(obj, ColumnProfile):
            profile = obj.table_profile
        else:
            profile = obj.column_profile.table_profile
        released = TableProfile.objects.values_list("is_released", flat=True).get(pk=profile.pk)
        return profile.schema_name, profile.table_name, released

    def save_model(self, request, obj, form, change):
        old = type(obj).objects.filter(pk=obj.pk).first() if change else None
        request._dbtool_admin_before = self._profile_state(old) if old else None
        request._dbtool_admin_changed = form.has_changed()
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        changed = request._dbtool_admin_changed or any(item.has_changed() for item in formsets)
        super().save_related(request, form, formsets, change)
        if changed:
            before = request._dbtool_admin_before
            after = self._profile_state(form.instance)
            for schema, table in {(state[0], state[1]) for state in (before, after) if state}:
                ConfigurationEvent.objects.create(
                    schema_name=schema, table_name=table,
                    action=ConfigurationEvent.Action.CONFIG_CHANGED,
                    actor=request.user,
                    was_released=bool(before and before[:2] == (schema, table) and before[2]),
                    is_released=bool(after[:2] == (schema, table) and after[2]),
                )

    def delete_model(self, request, obj):
        schema, table, released = self._profile_state(obj)
        super().delete_model(request, obj)
        ConfigurationEvent.objects.create(
            schema_name=schema, table_name=table,
            action=ConfigurationEvent.Action.CONFIG_CHANGED,
            actor=request.user, was_released=released, is_released=False,
        )


@admin.register(TableProfile)
class TableProfileAdmin(RootConfigurationAdmin):
    list_display = (
        "schema_name",
        "table_name",
        "display_label",
        "mutation_enabled",
    )
    list_filter = ("mutation_enabled",)
    search_fields = ("schema_name", "table_name", "display_label")
    exclude = ("is_released",)
    inlines = (ColumnProfileInline,)


@admin.register(ColumnProfile)
class ColumnProfileAdmin(RootConfigurationAdmin):
    list_display = (
        "table_profile",
        "column_name",
        "input_mode",
        "show_in_form",
        "editable",
        "required_on_insert",
        "reviewed",
    )
    list_filter = ("reviewed", "input_mode", "show_in_form", "editable")
    search_fields = (
        "table_profile__schema_name",
        "table_profile__table_name",
        "column_name",
        "display_label",
    )


@admin.register(LookupMapping)
class LookupMappingAdmin(RootConfigurationAdmin):
    list_display = (
        "column_profile",
        "target_schema",
        "target_table",
        "target_value_column",
        "allow_null",
        "allow_arbitrary_value",
    )
    search_fields = (
        "column_profile__table_profile__schema_name",
        "column_profile__table_profile__table_name",
        "column_profile__column_name",
        "target_schema",
        "target_table",
    )


@admin.register(ConfigurationEvent)
class ConfigurationEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at", "schema_name", "table_name", "action", "actor",
        "was_released", "is_released",
    )
    list_filter = ("action",)
    search_fields = ("schema_name", "table_name")

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
