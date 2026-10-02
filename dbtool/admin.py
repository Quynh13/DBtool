from django.contrib import admin

from dbtool.models import ColumnProfile, LookupMapping, TableProfile


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
