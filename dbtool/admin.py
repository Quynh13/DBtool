from django.contrib import admin

from dbtool.models import ColumnProfile, ConfigurationEvent, LookupMapping, TableProfile


admin.site.index_template = "dbtool/admin_index.html"


class ColumnProfileInline(admin.TabularInline):
    model = ColumnProfile
    extra = 0


@admin.register(TableProfile)
class TableProfileAdmin(admin.ModelAdmin):
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
class ColumnProfileAdmin(admin.ModelAdmin):
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
class LookupMappingAdmin(admin.ModelAdmin):
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
