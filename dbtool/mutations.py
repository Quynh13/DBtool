from django.db import transaction

from audit.codec import encode_row
from audit.models import OperationLog
from dbtool.introspection import SchemaIntrospector
from dbtool.repository import GenericTableRepository
from dbtool.row_state import row_fingerprint
from dbtool.semantic_validation import SemanticValidationError, SemanticValidator
from dbtool.semantics import EffectiveMetadataService


class MutationNotAllowed(Exception):
    pass


class RowNotFound(Exception):
    pass


class StaleRowError(Exception):
    pass


class MutationService:
    def __init__(self):
        self.introspector = SchemaIntrospector()
        self.repository = GenericTableRepository()
        self.metadata = EffectiveMetadataService(introspector=self.introspector)
        self.semantic_validator = SemanticValidator(self.repository)

    def insert(
        self,
        *,
        schema: str,
        table: str,
        values: dict,
        actor,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        effective = self.metadata.resolve(definition)
        capabilities = effective.capabilities

        if not capabilities.can_insert:
            raise MutationNotAllowed("INSERT is not allowed for this table.")

        allowed_columns = {
            name
            for name, capability in capabilities.columns.items()
            if capability.can_insert
        }

        unknown_columns = set(values) - allowed_columns

        if unknown_columns:
            raise MutationNotAllowed(
                "Attempted to write non-editable columns: "
                + ", ".join(sorted(unknown_columns))
            )

        try:
            self.semantic_validator.validate_insert(effective, values)
        except SemanticValidationError as exc:
            raise MutationNotAllowed(str(exc)) from exc

        operation = OperationLog.objects.create(
            schema_name=schema,
            table_name=table,
            action=OperationLog.Action.INSERT,
            status=OperationLog.Status.PENDING,
            actor=actor,
        )

        try:
            with transaction.atomic(using="target"):
                after = self.repository.insert_row(
                    schema,
                    table,
                    values,
                )

        except Exception as exc:
            operation.status = OperationLog.Status.FAILED

            operation.error_message = str(exc)[:4000]

            operation.save(
                update_fields=[
                    "status",
                    "error_message",
                ]
            )

            raise

        record_key = {
            column_name: after[column_name] for column_name in definition.primary_key
        }

        operation.record_key = encode_row(record_key)

        operation.after_data = encode_row(after)

        operation.status = OperationLog.Status.APPLIED

        operation.save(
            update_fields=[
                "record_key",
                "after_data",
                "status",
            ]
        )

        return after, operation

    def update(
        self,
        *,
        schema: str,
        table: str,
        record_key: dict,
        values: dict,
        expected_fingerprint: str,
        actor,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        effective = self.metadata.resolve(definition)
        capabilities = effective.capabilities

        if not capabilities.can_update:
            raise MutationNotAllowed("UPDATE is not allowed for this table.")

        allowed_columns = {
            name
            for name, capability in capabilities.columns.items()
            if capability.can_update
        }

        unknown_columns = set(values) - allowed_columns

        if unknown_columns:
            raise MutationNotAllowed(
                "Attempted to write non-editable columns: "
                + ", ".join(sorted(unknown_columns))
            )

        operation = None

        try:
            with transaction.atomic(using="target"):
                before = self.repository.get_row(
                    schema,
                    table,
                    record_key,
                    for_update=True,
                )

                if before is None:
                    raise RowNotFound("Row no longer exists.")

                if row_fingerprint(before) != expected_fingerprint:
                    raise StaleRowError(
                        "Row changed after the form was opened. Reload before updating."
                    )

                try:
                    self.semantic_validator.validate_update(
                        effective,
                        before,
                        values,
                    )
                except SemanticValidationError as exc:
                    raise MutationNotAllowed(str(exc)) from exc

                operation = OperationLog.objects.create(
                    schema_name=schema,
                    table_name=table,
                    action=(OperationLog.Action.UPDATE),
                    record_key=encode_row(record_key),
                    before_data=encode_row(before),
                    status=(OperationLog.Status.PENDING),
                    actor=actor,
                )

                after = self.repository.update_row(
                    schema,
                    table,
                    record_key,
                    values,
                )

                if after is None:
                    raise RowNotFound("Row disappeared during UPDATE.")

        except (
            RowNotFound,
            StaleRowError,
        ):
            raise

        except Exception as exc:
            if operation is not None:
                operation.status = OperationLog.Status.FAILED

                operation.error_message = str(exc)[:4000]

                operation.save(
                    update_fields=[
                        "status",
                        "error_message",
                    ]
                )

            raise

        operation.after_data = encode_row(after)

        operation.status = OperationLog.Status.APPLIED

        operation.save(
            update_fields=[
                "after_data",
                "status",
            ]
        )

        return after, operation

    def delete(
        self,
        *,
        schema: str,
        table: str,
        record_key: dict,
        expected_fingerprint: str,
        actor,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        effective = self.metadata.resolve(definition)
        capabilities = effective.capabilities

        if not capabilities.can_delete:
            raise MutationNotAllowed("DELETE is not allowed for this table.")

        operation = None

        try:
            with transaction.atomic(using="target"):
                before = self.repository.get_row(
                    schema,
                    table,
                    record_key,
                    for_update=True,
                )

                if before is None:
                    raise RowNotFound("Row no longer exists.")

                if row_fingerprint(before) != expected_fingerprint:
                    raise StaleRowError(
                        "Row changed after the delete page "
                        "was opened. Reload before deleting."
                    )

                operation = OperationLog.objects.create(
                    schema_name=schema,
                    table_name=table,
                    action=(OperationLog.Action.DELETE),
                    record_key=encode_row(record_key),
                    before_data=encode_row(before),
                    status=(OperationLog.Status.PENDING),
                    actor=actor,
                )

                deleted = self.repository.delete_row(
                    schema,
                    table,
                    record_key,
                )

                if deleted is None:
                    raise RowNotFound("Row disappeared during DELETE.")

        except (
            RowNotFound,
            StaleRowError,
        ):
            raise

        except Exception as exc:
            if operation is not None:
                operation.status = OperationLog.Status.FAILED

                operation.error_message = str(exc)[:4000]

                operation.save(
                    update_fields=[
                        "status",
                        "error_message",
                    ]
                )

            raise

        operation.status = OperationLog.Status.APPLIED

        operation.save(
            update_fields=[
                "status",
            ]
        )

        return deleted, operation
