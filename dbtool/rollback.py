from django.db import transaction
from django.utils import timezone

from audit.codec import (
    decode_row,
    encode_row,
)
from audit.models import OperationLog
from dbtool.repository import GenericTableRepository


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

    def rollback(
        self,
        operation: OperationLog,
    ):
        if operation.status not in {
            OperationLog.Status.APPLIED,
            OperationLog.Status.ROLLBACK_FAILED,
        }:
            raise RollbackNotAllowed(
                "This operation cannot be rolled back "
                f"from status '{operation.status}'."
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

            raise

        operation.status = OperationLog.Status.ROLLED_BACK

        operation.rolled_back_at = timezone.now()

        operation.error_message = ""

        operation.save(
            update_fields=[
                "status",
                "rolled_back_at",
                "error_message",
            ]
        )

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
