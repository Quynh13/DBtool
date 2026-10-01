from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from audit.models import OperationLog


@admin.register(OperationLog)
class OperationLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "action",
        "schema_name",
        "table_name",
        "status",
        "actor",
        "rollback_link",
    )

    list_filter = (
        "action",
        "status",
        "schema_name",
    )

    search_fields = (
        "schema_name",
        "table_name",
    )

    readonly_fields = (
        "id",
        "schema_name",
        "table_name",
        "action",
        "record_key",
        "before_data",
        "after_data",
        "status",
        "actor",
        "created_at",
        "rolled_back_at",
        "error_message",
    )

    def rollback_link(
        self,
        obj,
    ):
        if obj.status not in {
            OperationLog.Status.APPLIED,
            OperationLog.Status.ROLLBACK_FAILED,
        }:
            return "—"

        url = reverse(
            "dbtool:operation_rollback",
            kwargs={
                "operation_id": obj.pk,
            },
        )

        return format_html(
            '<a href="{}">Rollback</a>',
            url,
        )

    rollback_link.short_description = "Rollback"

    def has_add_permission(
        self,
        request,
    ):
        return False

    def has_delete_permission(
        self,
        request,
        obj=None,
    ):
        return False
