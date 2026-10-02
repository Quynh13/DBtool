from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

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
from dbtool.models import ColumnProfile, LookupMapping, TableProfile
from dbtool.semantic_validation import (
    SemanticValidationError,
    SemanticValidator,
)
from dbtool.semantics import (
    NEEDS_REVIEW,
    READY,
    EffectiveMetadataService,
)
from dbtool.safety import SafetyPolicy


class UrlRoutingTests(SimpleTestCase):
    def test_admin_model_urls_do_not_collide_with_generic_editor(self):
        admin_url = reverse("admin:dbtool_tableprofile_add")

        self.assertEqual(admin_url, "/admin/dbtool/tableprofile/add/")
        self.assertEqual(
            resolve(admin_url).url_name,
            "dbtool_tableprofile_add",
        )
        self.assertEqual(
            reverse("dbtool:table_list"),
            "/admin/db-editor/",
        )


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


class StubIntrospector:
    def __init__(self, *definitions):
        self.definitions = {
            (definition.schema, definition.name): definition
            for definition in definitions
        }

    def inspect_table(self, schema, table):
        try:
            return self.definitions[(schema, table)]
        except KeyError as exc:
            raise ValueError("Table does not exist.") from exc

    def list_tables(self):
        return [
            (definition.schema, definition.name, definition.table_type)
            for definition in self.definitions.values()
        ]


class SemanticFixture:
    @classmethod
    def setUpTestData(cls):
        cls.attendance = TableDefinition(
            schema="public",
            name="attendance",
            table_type="BASE TABLE",
            primary_key=["id"],
            columns=[
                ColumnDefinition(
                    name="id",
                    data_type="integer",
                    nullable=False,
                    default=None,
                    primary_key=True,
                    identity=True,
                    generated=False,
                ),
                ColumnDefinition(
                    name="employee_code",
                    data_type="text",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
                ColumnDefinition(
                    name="note",
                    data_type="text",
                    nullable=True,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )
        cls.employee = TableDefinition(
            schema="public",
            name="employee",
            table_type="BASE TABLE",
            primary_key=["code"],
            columns=[
                ColumnDefinition(
                    name="code",
                    data_type="text",
                    nullable=False,
                    default=None,
                    primary_key=True,
                    identity=False,
                    generated=False,
                ),
                ColumnDefinition(
                    name="name",
                    data_type="text",
                    nullable=False,
                    default=None,
                    primary_key=False,
                    identity=False,
                    generated=False,
                ),
            ],
        )

    def service(self):
        return EffectiveMetadataService(
            introspector=StubIntrospector(self.attendance, self.employee)
        )

    def configure_attendance(self, *, required=True, allow_null=False):
        table = TableProfile.objects.create(
            schema_name="public",
            table_name="attendance",
            display_label="Attendance",
            mutation_enabled=True,
        )
        ColumnProfile.objects.create(
            table_profile=table,
            column_name="id",
            show_in_form=False,
            editable=False,
            reviewed=True,
        )
        employee = ColumnProfile.objects.create(
            table_profile=table,
            column_name="employee_code",
            display_label="Employee",
            required_on_insert=required,
            allow_clear_on_update=allow_null,
            input_mode=ColumnProfile.InputMode.LOOKUP,
            reviewed=True,
        )
        ColumnProfile.objects.create(
            table_profile=table,
            column_name="note",
            display_label="Note",
            reviewed=True,
        )
        LookupMapping.objects.create(
            column_profile=employee,
            target_schema="public",
            target_table="employee",
            target_value_column="code",
            display_columns=["code", "name"],
            search_columns=["code", "name"],
            allow_null=allow_null,
            allow_arbitrary_value=False,
        )


class SemanticMetadataTests(SemanticFixture, TestCase):

    def test_unconfigured_mutable_table_needs_review_and_is_read_only(self):
        effective = self.service().resolve(self.attendance)

        self.assertEqual(effective.status, NEEDS_REVIEW)
        self.assertFalse(effective.capabilities.can_insert)
        self.assertFalse(effective.capabilities.can_update)
        self.assertFalse(effective.capabilities.can_delete)

    def test_nullable_column_can_be_semantically_required_lookup(self):
        self.configure_attendance()

        effective = self.service().resolve(self.attendance)
        employee = effective.columns["employee_code"]

        self.assertEqual(effective.status, READY)
        self.assertTrue(employee.required_on_insert)
        self.assertFalse(employee.allow_clear_on_update)
        self.assertIsNotNone(employee.lookup)
        self.assertTrue(effective.capabilities.can_insert)

    def test_optional_lookup_allows_null(self):
        self.configure_attendance(required=False, allow_null=True)

        employee = self.service().resolve(self.attendance).columns[
            "employee_code"
        ]

        self.assertFalse(employee.required_on_insert)
        self.assertTrue(employee.allow_clear_on_update)

    def test_incomplete_lookup_mapping_needs_review(self):
        self.configure_attendance()
        LookupMapping.objects.update(display_columns=[])

        effective = self.service().resolve(self.attendance)

        self.assertEqual(effective.status, NEEDS_REVIEW)
        self.assertFalse(effective.capabilities.can_insert)

    def test_unreviewed_column_keeps_table_in_review(self):
        self.configure_attendance()
        ColumnProfile.objects.filter(column_name="note").update(reviewed=False)

        effective = self.service().resolve(self.attendance)

        self.assertEqual(effective.status, NEEDS_REVIEW)
        self.assertFalse(effective.capabilities.can_insert)


@override_settings(
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
)
class ReviewFlowTests(SemanticFixture, TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password",
        )
        self.client.force_login(self.user)
        self.introspector = StubIntrospector(self.attendance, self.employee)

    def test_navigation_reaches_review_for_unconfigured_table(self):
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(reverse("dbtool:table_list"))
            review_url = reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "attendance"},
            )

        self.assertContains(response, review_url)
        self.assertContains(response, "Review")

        admin_response = self.client.get(reverse("admin:index"))
        self.assertContains(admin_response, reverse("dbtool:table_list"))

    def test_review_prefills_physical_metadata_without_identifier_inputs(self):
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(
                reverse(
                    "dbtool:table_review",
                    kwargs={"schema": "public", "table": "attendance"},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review public.attendance")
        self.assertContains(response, "employee_code")
        self.assertContains(response, "nullable=yes")
        self.assertNotIn("schema_name", response.context["table_form"].fields)
        self.assertNotIn("table_name", response.context["table_form"].fields)

    def test_review_save_creates_lookup_and_makes_table_ready(self):
        url = reverse(
            "dbtool:table_review",
            kwargs={"schema": "public", "table": "attendance"},
        )
        data = {
            "table-display_label": "Attendance",
            "table-mutation_enabled": "on",
            "column-0-reviewed": "on",
            "column-0-display_label": "Id",
            "column-0-input_mode": "text",
            "column-0-help_text": "",
            "column-1-reviewed": "on",
            "column-1-display_label": "Employee",
            "column-1-show_in_form": "on",
            "column-1-editable": "on",
            "column-1-required_on_insert": "on",
            "column-1-input_mode": "lookup",
            "column-1-help_text": "Choose an employee",
            "column-1-target_schema": "public",
            "column-1-target_table": "employee",
            "column-1-target_value_column": "code",
            "column-1-display_columns": ["code", "name"],
            "column-1-search_columns": ["code", "name"],
            "column-2-reviewed": "on",
            "column-2-display_label": "Note",
            "column-2-show_in_form": "on",
            "column-2-editable": "on",
            "column-2-allow_clear_on_update": "on",
            "column-2-input_mode": "text",
            "column-2-help_text": "",
        }

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.post(url, data)

        self.assertRedirects(response, url, fetch_redirect_response=False)
        profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        self.assertEqual(profile.column_profiles.count(), 3)
        lookup = LookupMapping.objects.get(
            column_profile__table_profile=profile,
            column_profile__column_name="employee_code",
        )
        self.assertEqual(lookup.target_table, "employee")
        self.assertEqual(lookup.display_columns, ["code", "name"])
        self.assertEqual(self.service().resolve(self.attendance).status, READY)


class SemanticValidationTests(SemanticFixture, TestCase):
    def setUp(self):
        from unittest.mock import Mock

        self.repository = Mock()
        self.validator = SemanticValidator(self.repository)
        self.configure_attendance()
        self.effective = self.service().resolve(self.attendance)

    def test_insert_requires_semantic_value_even_when_db_is_nullable(self):
        with self.assertRaisesMessage(
            SemanticValidationError,
            "Employee is required",
        ):
            self.validator.validate_insert(self.effective, {"note": "A"})

    def test_invalid_arbitrary_lookup_value_is_blocked(self):
        self.repository.lookup_value_exists.return_value = False

        with self.assertRaisesMessage(
            SemanticValidationError,
            "Select a valid value for Employee",
        ):
            self.validator.validate_insert(
                self.effective,
                {"employee_code": "INVALID", "note": None},
            )

    def test_legacy_invalid_unchanged_value_allows_unrelated_update(self):
        self.validator.validate_update(
            self.effective,
            {"employee_code": None, "note": "A"},
            {"employee_code": None, "note": "B"},
        )

        self.repository.lookup_value_exists.assert_not_called()

    def test_clearing_existing_required_relation_is_blocked(self):
        with self.assertRaisesMessage(
            SemanticValidationError,
            "Employee cannot be cleared",
        ):
            self.validator.validate_update(
                self.effective,
                {"employee_code": "NV001", "note": "A"},
                {"employee_code": None, "note": "A"},
            )

    def test_changing_relation_to_valid_target_is_allowed(self):
        self.repository.lookup_value_exists.return_value = True

        self.validator.validate_update(
            self.effective,
            {"employee_code": "NV001", "note": "A"},
            {"employee_code": "NV002", "note": "A"},
        )

        self.repository.lookup_value_exists.assert_called_once_with(
            "public",
            "employee",
            "code",
            "NV002",
        )
