from django.db import transaction

from audit.codec import encode_row
from audit.models import OperationLog, ToolOwnedRecord
from dbtool.introspection import SchemaIntrospector
from dbtool.repository import GenericTableRepository
from dbtool.ownership import key_digest, owned_record
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

    def _apply_lookup_outputs(self, effective, values, before=None):
        values = values.copy()
        for name, column in effective.columns.items():
            lookup = column.lookup
            if lookup is None or not lookup.output_mappings:
                continue
            for pair in lookup.output_mappings:
                values.pop(pair["destination"], None)
            selected = values.get(name, before.get(name) if before else None)
            if selected in (None, ""):
                raise MutationNotAllowed(f"Select a valid value for {column.label}.")
            if before is not None and selected == before.get(name):
                continue
            try:
                source = self.repository.get_row(
                    lookup.target_schema,
                    lookup.target_table,
                    {lookup.target_value_column: selected},
                )
            except Exception as exc:
                raise MutationNotAllowed(
                    f"Selected {column.label} could not be verified."
                ) from exc
            if source is None:
                raise MutationNotAllowed(f"Selected {column.label} no longer exists.")
            if source.get(lookup.target_value_column) != selected:
                raise MutationNotAllowed(f"Selected {column.label} could not be verified.")
            for pair in lookup.output_mappings:
                value = source.get(pair["source"])
                if value is None or value == "":
                    raise MutationNotAllowed(f"Selected {column.label} has incomplete data.")
                values[pair["destination"]] = value
        return values

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

        operation = None
        try:
            with transaction.atomic(using="target"):
                values = self._apply_lookup_outputs(effective, values)
                allowed_columns = {
                    name for name, capability in capabilities.columns.items()
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
                after = self.repository.insert_row(schema, table, values)
                record_key = {
                    column_name: after[column_name] for column_name in definition.primary_key
                }
                # Persist recovery evidence before the Target transaction commits.
                operation.record_key = encode_row(record_key)
                operation.after_data = encode_row(after)
                operation.save(update_fields=["record_key", "after_data"])
        except Exception as exc:
            if operation is not None:
                operation.status = OperationLog.Status.FAILED
                operation.error_message = str(exc)[:4000]
                operation.save(update_fields=["status", "error_message"])
            raise

        from dbtool.row_state import row_fingerprint
        with transaction.atomic():
            existing = ToolOwnedRecord.objects.filter(
                schema_name=schema, table_name=table, key_digest=key_digest(record_key)
            ).first()
            if existing is not None and existing.active:
                raise MutationNotAllowed("Record key is already registered to this tool.")
            if existing is None:
                ToolOwnedRecord.objects.create(
                    schema_name=schema, table_name=table,
                    key_digest=key_digest(record_key), record_key=encode_row(record_key),
                    fingerprint=row_fingerprint(after), origin=operation,
                    last_operation=operation,
                )
            else:
                existing.record_key = encode_row(record_key)
                existing.fingerprint = row_fingerprint(after)
                existing.origin = operation
                existing.last_operation = operation
                existing.active = True
                existing.save(update_fields=["record_key", "fingerprint", "origin", "last_operation", "active"])
            operation.status = OperationLog.Status.APPLIED
            operation.save(update_fields=["status"])

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

        owned = owned_record(schema, table, record_key)
        if owned is None:
            raise MutationNotAllowed("Only rows created by this tool may be updated.")

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

                if row_fingerprint(before) != owned.fingerprint:
                    raise StaleRowError("Owned row changed outside the tool.")

                if row_fingerprint(before) != expected_fingerprint:
                    raise StaleRowError(
                        "Row changed after the form was opened. Reload before updating."
                    )

                values = self._apply_lookup_outputs(effective, values, before)

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

                operation.after_data = encode_row(after)
                operation.save(update_fields=["after_data"])

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

        with transaction.atomic():
            owned.fingerprint = row_fingerprint(after)
            owned.last_operation = operation
            owned.save(update_fields=["fingerprint", "last_operation"])
            operation.status = OperationLog.Status.APPLIED
            operation.save(update_fields=["status"])

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

        owned = owned_record(schema, table, record_key)
        if owned is None:
            raise MutationNotAllowed("Only rows created by this tool may be deleted.")

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

                if row_fingerprint(before) != owned.fingerprint:
                    raise StaleRowError("Owned row changed outside the tool.")

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

        with transaction.atomic():
            owned.active = False
            owned.last_operation = operation
            owned.save(update_fields=["active", "last_operation"])
            operation.status = OperationLog.Status.APPLIED
            operation.save(update_fields=["status"])

        return deleted, operation
