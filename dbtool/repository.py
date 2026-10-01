from django.db import connections

from dbtool.introspection import SchemaIntrospector


class GenericTableRepository:
    database_alias = "target"

    def __init__(self):
        self.introspector = SchemaIntrospector()

    def _validate_record_key(
        self,
        definition,
        record_key: dict,
    ):
        expected = set(definition.primary_key)
        received = set(record_key.keys())

        if not expected:
            raise ValueError("Table has no primary key.")

        if received != expected:
            raise ValueError("Record key does not match table primary key.")

    def _validate_columns(
        self,
        definition,
        values: dict,
    ):
        known_columns = {column.name for column in definition.columns}

        unknown = set(values) - known_columns

        if unknown:
            raise ValueError("Unknown columns: " + ", ".join(sorted(unknown)))

    def list_rows(
        self,
        schema: str,
        table: str,
        limit: int = 100,
        offset: int = 0,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        order_clause = ""

        if definition.primary_key:
            quoted_pk = ", ".join(
                connection.ops.quote_name(name) for name in definition.primary_key
            )

            order_clause = f" ORDER BY {quoted_pk}"

        sql = f"""
            SELECT *
            FROM {quoted_schema}.{quoted_table}
            {order_clause}
            LIMIT %s
            OFFSET %s
        """

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                [limit, offset],
            )

            columns = [description[0] for description in cursor.description]

            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        return rows

    def count_rows(
        self,
        schema: str,
        table: str,
    ) -> int:
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        sql = f"""
            SELECT COUNT(*)
            FROM {quoted_schema}.{quoted_table}
        """

        with connection.cursor() as cursor:
            cursor.execute(sql)

            return cursor.fetchone()[0]

    def get_row(
        self,
        schema: str,
        table: str,
        record_key: dict,
        *,
        for_update: bool = False,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        self._validate_record_key(
            definition,
            record_key,
        )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        where_clause = " AND ".join(
            (f"{connection.ops.quote_name(name)} = %s")
            for name in definition.primary_key
        )

        params = [record_key[name] for name in definition.primary_key]

        lock_clause = " FOR UPDATE" if for_update else ""

        sql = f"""
            SELECT *
            FROM {quoted_schema}.{quoted_table}
            WHERE {where_clause}
            {lock_clause}
        """

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                params,
            )

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [description[0] for description in cursor.description]

        return dict(zip(columns, row))

    def insert_row(
        self,
        schema: str,
        table: str,
        values: dict,
    ) -> dict:
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        self._validate_columns(
            definition,
            values,
        )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        if values:
            column_names = list(values.keys())

            quoted_columns = ", ".join(
                connection.ops.quote_name(name) for name in column_names
            )

            placeholders = ", ".join(["%s"] * len(column_names))

            sql = f"""
                INSERT INTO {quoted_schema}.{quoted_table}
                    ({quoted_columns})
                VALUES ({placeholders})
                RETURNING *
            """

            params = [values[name] for name in column_names]

        else:
            sql = f"""
                INSERT INTO {quoted_schema}.{quoted_table}
                DEFAULT VALUES
                RETURNING *
            """

            params = []

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                params,
            )

            row = cursor.fetchone()

            columns = [description[0] for description in cursor.description]

        return dict(zip(columns, row))

    def update_row(
        self,
        schema: str,
        table: str,
        record_key: dict,
        values: dict,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        self._validate_record_key(
            definition,
            record_key,
        )

        self._validate_columns(
            definition,
            values,
        )

        if not values:
            raise ValueError("No values supplied for UPDATE.")

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        set_clause = ", ".join(
            f"{connection.ops.quote_name(name)} = %s" for name in values
        )

        where_clause = " AND ".join(
            (f"{connection.ops.quote_name(name)} = %s")
            for name in definition.primary_key
        )

        params = [values[name] for name in values]

        params.extend(record_key[name] for name in definition.primary_key)

        sql = f"""
            UPDATE {quoted_schema}.{quoted_table}
            SET {set_clause}
            WHERE {where_clause}
            RETURNING *
        """

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                params,
            )

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [description[0] for description in cursor.description]

        return dict(zip(columns, row))

    def delete_row(
        self,
        schema: str,
        table: str,
        record_key: dict,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        self._validate_record_key(
            definition,
            record_key,
        )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        where_clause = " AND ".join(
            (f"{connection.ops.quote_name(name)} = %s")
            for name in definition.primary_key
        )

        params = [record_key[name] for name in definition.primary_key]

        sql = f"""
            DELETE FROM {quoted_schema}.{quoted_table}
            WHERE {where_clause}
            RETURNING *
        """

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                params,
            )

            row = cursor.fetchone()

            if row is None:
                return None

            columns = [description[0] for description in cursor.description]

        return dict(zip(columns, row))

    def restore_deleted_row(
        self,
        schema: str,
        table: str,
        snapshot: dict,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        expected_columns = {column.name for column in definition.columns}

        if set(snapshot.keys()) != expected_columns:
            raise ValueError(
                "Table schema has changed since the operation. Rollback is blocked."
            )

        connection = connections[self.database_alias]

        quoted_schema = connection.ops.quote_name(definition.schema)

        quoted_table = connection.ops.quote_name(definition.name)

        insert_columns = [
            column for column in definition.columns if not column.generated
        ]

        quoted_columns = ", ".join(
            connection.ops.quote_name(column.name) for column in insert_columns
        )

        placeholders = ", ".join(["%s"] * len(insert_columns))

        overriding = ""

        if any(column.identity for column in insert_columns):
            overriding = " OVERRIDING SYSTEM VALUE"

        sql = f"""
            INSERT INTO {quoted_schema}.{quoted_table}
                ({quoted_columns})
            {overriding}
            VALUES ({placeholders})
            RETURNING *
        """

        params = [snapshot[column.name] for column in insert_columns]

        with connection.cursor() as cursor:
            cursor.execute(
                sql,
                params,
            )

            row = cursor.fetchone()

            columns = [description[0] for description in cursor.description]

        return dict(zip(columns, row))

    def restore_updated_row(
        self,
        schema: str,
        table: str,
        record_key: dict,
        snapshot: dict,
    ):
        definition = self.introspector.inspect_table(
            schema,
            table,
        )

        expected_columns = {column.name for column in definition.columns}

        if set(snapshot.keys()) != expected_columns:
            raise ValueError(
                "Table schema has changed since the operation. Rollback is blocked."
            )

        values = {
            column.name: snapshot[column.name]
            for column in definition.columns
            if (not column.primary_key and not column.generated)
        }

        if not values:
            return self.get_row(
                schema,
                table,
                record_key,
            )

        return self.update_row(
            schema,
            table,
            record_key,
            values,
        )
