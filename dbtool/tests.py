from django.core import signing
from django.test import SimpleTestCase

from dbtool.introspection import (
    ColumnDefinition,
    TableDefinition,
)
from dbtool.row_state import (
    load_row_token,
    load_state_token,
    make_row_token,
    make_state_token,
    row_fingerprint,
)
from dbtool.safety import SafetyPolicy


class SafetyPolicyTests(SimpleTestCase):
    def test_normal_base_table_with_pk_is_mutable(self):
        table = TableDefinition(
            schema="public",
            name="example",
            table_type="BASE TABLE",
            primary_key=["Id"],
            columns=[
                ColumnDefinition(
                    name="Id",
                    data_type="integer",
                    nullable=False,
                    default=None,
                    primary_key=True,
                    identity=True,
                    generated=False,
                ),
                ColumnDefinition(
                    name="Name",
                    data_type="text",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )

        capabilities = SafetyPolicy().evaluate(table)

        self.assertTrue(capabilities.can_read)

        self.assertTrue(capabilities.can_insert)

        self.assertTrue(capabilities.can_update)

        self.assertTrue(capabilities.can_delete)

        self.assertFalse(capabilities.columns["Id"].can_update)

        self.assertTrue(capabilities.columns["Name"].can_update)

    def test_view_is_read_only(self):
        table = TableDefinition(
            schema="public",
            name="example_view",
            table_type="VIEW",
            primary_key=[],
            columns=[
                ColumnDefinition(
                    name="Name",
                    data_type="text",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )

        capabilities = SafetyPolicy().evaluate(table)

        self.assertTrue(capabilities.can_read)

        self.assertFalse(capabilities.can_insert)

        self.assertFalse(capabilities.can_update)

        self.assertFalse(capabilities.can_delete)

    def test_table_without_pk_is_read_only_for_mutations(self):
        table = TableDefinition(
            schema="public",
            name="no_pk",
            table_type="BASE TABLE",
            primary_key=[],
            columns=[
                ColumnDefinition(
                    name="Name",
                    data_type="text",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )

        capabilities = SafetyPolicy().evaluate(table)

        self.assertFalse(capabilities.can_insert)

        self.assertFalse(capabilities.can_update)

        self.assertFalse(capabilities.can_delete)

    def test_bytea_is_not_editable(self):
        table = TableDefinition(
            schema="public",
            name="binary_table",
            table_type="BASE TABLE",
            primary_key=["Id"],
            columns=[
                ColumnDefinition(
                    name="Id",
                    data_type="integer",
                    nullable=False,
                    default=None,
                    primary_key=True,
                    identity=True,
                    generated=False,
                ),
                ColumnDefinition(
                    name="Payload",
                    data_type="bytea",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )

        capabilities = SafetyPolicy().evaluate(table)

        payload = capabilities.columns["Payload"]

        self.assertFalse(payload.can_insert)

        self.assertFalse(payload.can_update)


class RowStateTests(SimpleTestCase):
    def test_row_token_round_trip(self):
        record_key = {
            "Id": 123,
        }

        token = make_row_token(record_key)

        self.assertEqual(
            load_row_token(token),
            record_key,
        )

    def test_tampered_row_token_is_rejected(self):
        token = make_row_token(
            {
                "Id": 123,
            }
        )

        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

        with self.assertRaises(signing.BadSignature):
            load_row_token(tampered)

    def test_state_token_contains_key_and_fingerprint(self):
        record_key = {
            "Id": 123,
        }

        row = {
            "Id": 123,
            "Name": "Before",
        }

        token = make_state_token(
            record_key,
            row,
        )

        decoded_key, fingerprint = load_state_token(token)

        self.assertEqual(
            decoded_key,
            record_key,
        )

        self.assertEqual(
            fingerprint,
            row_fingerprint(row),
        )

    def test_fingerprint_changes_when_row_changes(self):
        before = {
            "Id": 123,
            "Name": "Before",
        }

        after = {
            "Id": 123,
            "Name": "After",
        }

        self.assertNotEqual(
            row_fingerprint(before),
            row_fingerprint(after),
        )
