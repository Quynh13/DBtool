from dataclasses import dataclass, field

from dbtool.introspection import ColumnDefinition, TableDefinition


EDITABLE_TYPES = {
    "smallint",
    "integer",
    "bigint",
    "numeric",
    "decimal",
    "real",
    "double precision",
    "text",
    "character varying",
    "character",
    "boolean",
    "date",
    "time without time zone",
    "time with time zone",
    "timestamp without time zone",
    "timestamp with time zone",
    "uuid",
    "json",
    "jsonb",
}


@dataclass(frozen=True)
class ColumnCapability:
    name: str
    can_insert: bool
    can_update: bool
    required_on_insert: bool
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TableCapabilities:
    can_read: bool
    can_insert: bool
    can_update: bool
    can_delete: bool
    warnings: list[str]
    columns: dict[str, ColumnCapability]


class SafetyPolicy:
    def evaluate(self, table: TableDefinition) -> TableCapabilities:
        warnings: list[str] = []
        column_capabilities: dict[str, ColumnCapability] = {}

        is_base_table = table.table_type == "BASE TABLE"
        has_primary_key = bool(table.primary_key)

        if not is_base_table:
            warnings.append(f"{table.table_type} is read-only in this MVP.")

        if not has_primary_key:
            warnings.append(
                "No primary key detected. All mutations are disabled because "
                "rows cannot be uniquely identified and safely rolled back."
            )

        insert_blocked = False
        has_updatable_column = False

        for column in table.columns:
            column_warnings: list[str] = []

            supported = column.data_type in EDITABLE_TYPES

            if not supported:
                column_warnings.append(
                    f"Datatype '{column.data_type}' has no editor in this MVP."
                )

            if column.identity:
                column_warnings.append("Identity column is database-managed.")

            if column.generated:
                column_warnings.append("Generated column is database-managed.")

            if column.primary_key:
                column_warnings.append(
                    "Primary key cannot be changed after row creation."
                )

            can_insert = (
                is_base_table
                and has_primary_key
                and supported
                and not column.identity
                and not column.generated
            )

            can_update = (
                is_base_table
                and has_primary_key
                and supported
                and not column.primary_key
                and not column.identity
                and not column.generated
            )

            if can_update:
                has_updatable_column = True

            required_on_insert = (
                not column.nullable
                and column.default is None
                and not column.identity
                and not column.generated
            )

            if required_on_insert and not can_insert:
                insert_blocked = True
                warnings.append(
                    f"INSERT disabled: required column '{column.name}' "
                    f"cannot be safely edited."
                )

            column_capabilities[column.name] = ColumnCapability(
                name=column.name,
                can_insert=can_insert,
                can_update=can_update,
                required_on_insert=required_on_insert,
                warnings=column_warnings,
            )

        return TableCapabilities(
            can_read=True,
            can_insert=(is_base_table and has_primary_key and not insert_blocked),
            can_update=(is_base_table and has_primary_key and has_updatable_column),
            can_delete=(is_base_table and has_primary_key),
            warnings=warnings,
            columns=column_capabilities,
        )
