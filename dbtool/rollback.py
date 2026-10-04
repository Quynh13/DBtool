from django.db import transaction
from django.utils import timezone

from audit.codec import (
    decode_row,
    encode_row,
)
from audit.models import OperationLog, ToolOwnedRecord
from dbtool.access import can_access_record, can_use_tool
from dbtool.introspection import SchemaIntrospector
from dbtool.models import ConfigurationEvent
from dbtool.ownership import key_digest, owned_record
from dbtool.row_state import row_fingerprint
from dbtool.repository import GenericTableRepository
from dbtool.semantics import EffectiveMetadataService


class RollbackNotAllowed(Exception):
    pass


class RollbackConflict(Exception):
    pass


def _rows_equal(
    left: dict,
    right: dict,
) -> bool:
    return encode_row(left) == encode_row(right)


class RollbackService:
    def __init__(self):
        self.repository = GenericTableRepository()

    def restore_deleted(self, operation: OperationLog, *, actor):
        if operation.action != OperationLog.Action.DELETE:
            raise RollbackNotAllowed("Only deleted records can be restored.")
        if not can_use_tool(actor):
            raise RollbackNotAllowed("Tool access is required.")
        record_key = decode_row(operation.record_key)
        if not record_key or not operation.record_origin_id:
            raise RollbackNotAllowed("Record ownership could not be verified.")
        owned = ToolOwnedRecord.objects.filter(
            schema_name=operation.schema_name,
            table_name=operation.table_name,
            key_digest=key_digest(record_key),
            active=False,
            origin_id=operation.record_origin_id,
            last_operation_id=operation.pk,
        ).select_related("origin").first()
        if not can_access_record(actor, owned):
            raise RollbackNotAllowed("Record ownership could not be verified.")
        if not actor.is_superuser and operation.actor_id != actor.pk:
            raise RollbackNotAllowed("Only your own deletion can be restored.")
        release = ConfigurationEvent.objects.filter(
            schema_name=operation.schema_name,
            table_name=operation.table_name,
            action=ConfigurationEvent.Action.RELEASE,
            visible_fields__isnull=False,
        ).order_by("-pk").first()
        if release is None or operation.form_release_id != release.pk:
            raise RollbackNotAllowed("The form has changed since deletion; restore is blocked.")
        introspector = SchemaIntrospector()
        try:
            definition = introspector.inspect_table(operation.schema_name, operation.table_name)
        except ValueError as exc:
            raise RollbackNotAllowed("The table no longer exists.") from exc
        effective = EffectiveMetadataService(introspector=introspector).resolve(definition)
        if not effective.capabilities.can_insert:
            raise RollbackNotAllowed("The table is not released and safe for restore.")
        self.rollback(operation, actor=actor, form_release=release)

    def rollback(
        self,
        operation: OperationLog,
        *,
        actor=None,
        form_release=None,
    ):
        if operation.status not in {
            OperationLog.Status.APPLIED,
            OperationLog.Status.ROLLBACK_FAILED,
        }:
            raise RollbackNotAllowed(
                "This operation cannot be rolled back "
                f"from status '{operation.status}'."
            )

        record_key = decode_row(operation.record_key)
        owned = owned_record(operation.schema_name, operation.table_name, record_key) if record_key else None
        if operation.action == OperationLog.Action.DELETE:
            owned = ToolOwnedRecord.objects.filter(
                schema_name=operation.schema_name,
                table_name=operation.table_name,
                key_digest=key_digest(record_key),
                active=False,
            ).first()
        if owned is None or owned.origin.action != OperationLog.Action.INSERT:
            raise RollbackNotAllowed("Only rows created by this tool may be rolled back.")
        if owned.last_operation_id != operation.id:
            raise RollbackNotAllowed("A later operation changed this record.")
        if operation.action == OperationLog.Action.INSERT and owned.origin_id != operation.id:
            raise RollbackNotAllowed("This INSERT no longer owns the current record.")

        restore_intent = None
        if actor is not None:
            if operation.action != OperationLog.Action.DELETE:
                raise RollbackNotAllowed("Only deleted records can be restored.")
            restore_intent = OperationLog.objects.create(
                schema_name=operation.schema_name,
                table_name=operation.table_name,
                action=OperationLog.Action.RESTORE,
                record_key=operation.record_key,
                record_origin=owned.origin,
                form_release=form_release,
                after_data=operation.before_data,
                actor=actor,
            )

        operation.status = OperationLog.Status.ROLLBACK_PENDING

        operation.error_message = ""

        operation.save(
            update_fields=[
                "status",
                "error_message",
            ]
        )

        try:
            if operation.action == OperationLog.Action.INSERT:
                self._rollback_insert(operation)

            elif operation.action == OperationLog.Action.UPDATE:
                self._rollback_update(operation)

            elif operation.action == OperationLog.Action.DELETE:
                self._rollback_delete(operation)

            else:
                raise RollbackNotAllowed("Unsupported operation type.")

        except Exception as exc:
            operation.status = OperationLog.Status.ROLLBACK_FAILED

            operation.error_message = str(exc)[:4000]

            operation.save(
                update_fields=[
                    "status",
                    "error_message",
                ]
            )
            if restore_intent:
                restore_intent.status = OperationLog.Status.FAILED
                restore_intent.error_message = str(exc)[:4000]
                restore_intent.save(update_fields=["status", "error_message"])

            raise

        operation.status = OperationLog.Status.ROLLED_BACK

        operation.rolled_back_at = timezone.now()

        operation.error_message = ""

        with transaction.atomic():
            if operation.action == OperationLog.Action.INSERT:
                owned.active = False
                owned.last_operation = operation
                owned.save(update_fields=["active", "last_operation"])
            elif operation.action == OperationLog.Action.DELETE:
                owned.active = True
                owned.fingerprint = row_fingerprint(decode_row(operation.before_data))
                owned.last_operation = restore_intent or operation
                owned.save(update_fields=["active", "fingerprint", "last_operation"])
            elif operation.action == OperationLog.Action.UPDATE:
                owned.fingerprint = row_fingerprint(decode_row(operation.before_data))
                owned.last_operation = operation
                owned.save(update_fields=["fingerprint", "last_operation"])
            operation.save(update_fields=["status", "rolled_back_at", "error_message"])
            if restore_intent:
                restore_intent.status = OperationLog.Status.APPLIED
                restore_intent.save(update_fields=["status"])

    def _rollback_insert(
        self,
        operation,
    ):
        record_key = decode_row(operation.record_key)

        expected_after = decode_row(operation.after_data)

        if record_key is None or expected_after is None:
            raise RollbackNotAllowed("INSERT rollback snapshot is incomplete.")

        with transaction.atomic(using="target"):
            current = self.repository.get_row(
                operation.schema_name,
                operation.table_name,
                record_key,
                for_update=True,
            )

            if current is None:
                raise RollbackConflict("Inserted row no longer exists.")

            owned = owned_record(operation.schema_name, operation.table_name, record_key)
            if owned is None or owned.fingerprint != row_fingerprint(current):
                raise RollbackConflict("Owned row changed since the operation.")

            if not _rows_equal(
                current,
                expected_after,
            ):
                raise RollbackConflict(
                    "Inserted row has changed since the operation. Rollback blocked."
                )

            deleted = self.repository.delete_row(
                operation.schema_name,
                operation.table_name,
                record_key,
            )

            if deleted is None:
                raise RollbackConflict("Row disappeared during rollback.")

    def _rollback_update(
        self,
        operation,
    ):
        record_key = decode_row(operation.record_key)

        expected_before = decode_row(operation.before_data)

        expected_after = decode_row(operation.after_data)

        if record_key is None or expected_before is None or expected_after is None:
            raise RollbackNotAllowed("UPDATE rollback snapshot is incomplete.")

        with transaction.atomic(using="target"):
            current = self.repository.get_row(
                operation.schema_name,
                operation.table_name,
                record_key,
                for_update=True,
            )

            if current is None:
                raise RollbackConflict("Updated row no longer exists.")

            owned = owned_record(operation.schema_name, operation.table_name, record_key)
            if owned is None or owned.fingerprint != row_fingerprint(current):
                raise RollbackConflict("Owned row changed since the operation.")

            if not _rows_equal(
                current,
                expected_after,
            ):
                raise RollbackConflict(
                    "Row has changed since UPDATE. Rollback blocked."
                )

            restored = self.repository.restore_updated_row(
                operation.schema_name,
                operation.table_name,
                record_key,
                expected_before,
            )

            if restored is None:
                raise RollbackConflict("Row disappeared during rollback.")

            if not _rows_equal(
                restored,
                expected_before,
            ):
                raise RollbackConflict(
                    "Database did not restore the exact previous row state."
                )

    def _rollback_delete(
        self,
        operation,
    ):
        record_key = decode_row(operation.record_key)

        expected_before = decode_row(operation.before_data)

        if record_key is None or expected_before is None:
            raise RollbackNotAllowed("DELETE rollback snapshot is incomplete.")

        with transaction.atomic(using="target"):
            current = self.repository.get_row(
                operation.schema_name,
                operation.table_name,
                record_key,
                for_update=True,
            )

            if current is not None:
                raise RollbackConflict(
                    "Primary key already exists. DELETE rollback blocked."
                )

            restored = self.repository.restore_deleted_row(
                operation.schema_name,
                operation.table_name,
                expected_before,
            )

            if not _rows_equal(
                restored,
                expected_before,
            ):
                raise RollbackConflict(
                    "Database did not restore the exact deleted row."
                )
