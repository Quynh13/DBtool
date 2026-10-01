import uuid

from django.conf import settings
from django.db import models


class OperationLog(models.Model):
    class Action(models.TextChoices):
        INSERT = "INSERT", "Insert"
        UPDATE = "UPDATE", "Update"
        DELETE = "DELETE", "Delete"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPLIED = "APPLIED", "Applied"
        FAILED = "FAILED", "Failed"
        ROLLBACK_PENDING = "ROLLBACK_PENDING", "Rollback pending"
        ROLLED_BACK = "ROLLED_BACK", "Rolled back"
        ROLLBACK_FAILED = "ROLLBACK_FAILED", "Rollback failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    schema_name = models.CharField(max_length=255)
    table_name = models.CharField(max_length=255)

    action = models.CharField(
        max_length=16,
        choices=Action.choices,
    )

    # Encoded representation of the PK / composite PK.
    record_key = models.JSONField(
        null=True,
        blank=True,
    )

    # Complete row snapshots.
    before_data = models.JSONField(
        null=True,
        blank=True,
    )

    after_data = models.JSONField(
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING,
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    rolled_back_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    error_message = models.TextField(
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.schema_name}.{self.table_name} {self.id}"
