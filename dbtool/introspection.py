from dataclasses import dataclass

from django.db import connections


@dataclass(frozen=True)
class ColumnDefinition:
    name: str
    data_type: str
    nullable: bool
    default: str | None
    primary_key: bool
    identity: bool
    generated: bool


@dataclass(frozen=True)
class TableDefinition:
    schema: str
    name: str
    table_type: str
    columns: list[ColumnDefinition]
    primary_key: list[str]

    @property
    def writable(self) -> bool:
        return self.table_type == "BASE TABLE"


class SchemaIntrospector:
    database_alias = "target"

    def __init__(self):
        self._definitions = {}

    def list_definitions(self):
        """Read the physical catalog in three queries, independent of table count."""
        connection = connections[self.database_alias]
        tables = self.list_tables()
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT tc.table_schema, tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
            """)
            keys = {}
            for schema, table, column in cursor.fetchall():
                keys.setdefault((schema, table), []).append(column)
            cursor.execute("""
                SELECT table_schema, table_name, column_name, data_type,
                       is_nullable, column_default, is_identity, is_generated
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name, ordinal_position
            """)
            columns = {}
            for schema, table, name, data_type, nullable, default, identity, generated in cursor.fetchall():
                columns.setdefault((schema, table), []).append(ColumnDefinition(
                    name=name, data_type=data_type, nullable=nullable == "YES",
                    default=default, primary_key=name in keys.get((schema, table), []),
                    identity=identity == "YES", generated=generated != "NEVER",
                ))
        definitions = [TableDefinition(schema, table, kind, columns.get((schema, table), []),
                                       keys.get((schema, table), [])) for schema, table, kind in tables]
        self._definitions = {(item.schema, item.name): item for item in definitions}
        return definitions

    def column_catalog(self):
        connection = connections[self.database_alias]
        catalog = {}
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name, ordinal_position
            """)
            for schema, table, name in cursor.fetchall():
                catalog.setdefault(schema, {}).setdefault(table, []).append(name)
        return catalog

    def list_tables(self) -> list[tuple[str, str, str]]:
        connection = connections[self.database_alias]

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    table_schema,
                    table_name,
                    table_type
                FROM information_schema.tables
                WHERE table_schema NOT IN (
                    'pg_catalog',
                    'information_schema'
                )
                ORDER BY table_schema, table_name
                """
            )

            return [
                (schema, table, table_type)
                for schema, table, table_type in cursor.fetchall()
            ]

    def inspect_table(
        self,
        schema: str,
        table: str,
    ) -> TableDefinition:
        if (schema, table) in self._definitions:
            return self._definitions[(schema, table)]
        connection = connections[self.database_alias]

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_type
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
                """,
                [schema, table],
            )

            row = cursor.fetchone()

            if row is None:
                raise ValueError("Table does not exist.")

            table_type = row[0]

            cursor.execute(
                """
                SELECT
                    kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.table_name = kcu.table_name
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = %s
                  AND tc.table_name = %s
                ORDER BY kcu.ordinal_position
                """,
                [schema, table],
            )

            primary_key = [row[0] for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT
                    column_name,
                    data_type,
                    is_nullable,
                    column_default,
                    is_identity,
                    is_generated
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                [schema, table],
            )

            columns = []

            for (
                name,
                data_type,
                is_nullable,
                default,
                is_identity,
                is_generated,
            ) in cursor.fetchall():
                columns.append(
                    ColumnDefinition(
                        name=name,
                        data_type=data_type,
                        nullable=is_nullable == "YES",
                        default=default,
                        primary_key=name in primary_key,
                        identity=is_identity == "YES",
                        generated=is_generated != "NEVER",
                    )
                )

        return TableDefinition(
            schema=schema,
            name=table,
            table_type=table_type,
            columns=columns,
            primary_key=primary_key,
        )
