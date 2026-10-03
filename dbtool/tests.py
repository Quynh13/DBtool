from contextlib import nullcontext
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
from dbtool.models import ColumnProfile, ConfigurationEvent, LookupMapping, TableProfile
from dbtool.forms import ColumnReviewForm, build_row_form
from dbtool.mutations import MutationNotAllowed, MutationService
from audit.codec import encode_row
from audit.models import OperationLog, ToolOwnedRecord
from dbtool.ownership import key_digest
from dbtool.rollback import RollbackNotAllowed, RollbackService
from dbtool.semantic_validation import (
    SemanticValidationError,
    SemanticValidator,
)
from dbtool.semantics import (
    COMPLETE,
    INCOMPLETE,
    NEEDS_REVIEW,
    NOT_CONFIGURED,
    READY,
    READ_ONLY,
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
class NavigationTests(TestCase):
    def setUp(self):
        self.root = get_user_model().objects.create_superuser(
            username="root-nav",
            email="root-nav@example.com",
            password="password",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="staff-nav",
            password="password",
            is_staff=True,
        )

    def test_root_navigation_links_editor_configuration_and_logs(self):
        self.client.force_login(self.root)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, reverse("dbtool:table_list"))
        self.assertContains(
            response,
            reverse("dbtool:database_configuration"),
        )
        self.assertContains(
            response,
            reverse("admin:audit_operationlog_changelist"),
        )

    def test_normal_user_only_sees_editor_navigation(self):
        self.client.force_login(self.normal_user)

        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, reverse("dbtool:table_list"))
        self.assertNotContains(
            response,
            reverse("dbtool:database_configuration"),
        )
        self.assertNotContains(
            response,
            reverse("admin:audit_operationlog_changelist"),
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

    def list_definitions(self):
        return list(self.definitions.values())

    def column_catalog(self):
        catalog = {}
        for definition in self.definitions.values():
            catalog.setdefault(definition.schema, {})[definition.name] = [
                column.name for column in definition.columns
            ]
        return catalog


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

    def test_new_profile_is_not_released(self):
        profile = TableProfile.objects.create(
            schema_name="public",
            table_name="new_table",
        )

        self.assertFalse(profile.is_released)

    def test_unconfigured_mutable_table_needs_review_and_is_read_only(self):
        effective = self.service().resolve(self.attendance)

        self.assertEqual(effective.configuration_status, NOT_CONFIGURED)
        self.assertEqual(effective.status, NEEDS_REVIEW)
        self.assertFalse(effective.capabilities.can_insert)
        self.assertFalse(effective.capabilities.can_update)
        self.assertFalse(effective.capabilities.can_delete)

    def test_partial_configuration_is_incomplete(self):
        TableProfile.objects.create(
            schema_name="public",
            table_name="attendance",
            mutation_enabled=True,
        )

        effective = self.service().resolve(self.attendance)

        self.assertEqual(effective.configuration_status, INCOMPLETE)

    def test_nullable_column_can_be_semantically_required_lookup(self):
        self.configure_attendance()

        effective = self.service().resolve(self.attendance)
        employee = effective.columns["employee_code"]

        self.assertEqual(effective.configuration_status, COMPLETE)
        self.assertEqual(effective.status, READY)
        self.assertTrue(employee.required_on_insert)
        self.assertFalse(employee.allow_clear_on_update)
        self.assertIsNotNone(employee.lookup)
        self.assertFalse(effective.profile.is_released)
        self.assertFalse(effective.capabilities.can_insert)
        self.assertFalse(effective.capabilities.can_update)
        self.assertFalse(effective.capabilities.can_delete)

        effective.profile.is_released = True
        effective.profile.save(update_fields=["is_released"])
        effective = self.service().resolve(self.attendance)

        self.assertTrue(effective.capabilities.can_insert)
        self.assertTrue(effective.capabilities.can_update)
        self.assertTrue(effective.capabilities.can_delete)

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

    def test_physically_read_only_table_has_read_only_configuration(self):
        view = TableDefinition(
            schema="public",
            name="attendance_view",
            table_type="VIEW",
            primary_key=[],
            columns=self.attendance.columns,
        )

        effective = EffectiveMetadataService(
            introspector=StubIntrospector(view)
        ).resolve(view)

        self.assertEqual(effective.configuration_status, READ_ONLY)

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
class DatabaseConfigurationTests(SemanticFixture, TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="root",
            email="root@example.com",
            password="password",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="staff",
            password="password",
            is_staff=True,
        )
        self.partial = TableDefinition(
            schema="public",
            name="partial",
            table_type="BASE TABLE",
            primary_key=self.attendance.primary_key,
            columns=self.attendance.columns,
        )
        self.read_only_view = TableDefinition(
            schema="public",
            name="attendance_view",
            table_type="VIEW",
            primary_key=[],
            columns=self.attendance.columns,
        )
        self.introspector = StubIntrospector(
            self.attendance,
            self.employee,
            self.partial,
            self.read_only_view,
        )
        self.url = reverse("dbtool:database_configuration")

    def test_superuser_sees_tables_statuses_release_state_and_actions(self):
        self.configure_attendance()
        TableProfile.objects.filter(
            schema_name="public",
            table_name="attendance",
        ).update(is_released=True)
        TableProfile.objects.create(
            schema_name="public",
            table_name="partial",
            mutation_enabled=True,
        )
        self.client.force_login(self.superuser)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        rows = {item["name"]: item for item in response.context["tables"]}
        self.assertEqual(rows["attendance"]["configuration_status"], COMPLETE)
        self.assertEqual(rows["attendance"]["release_status"], "RELEASED")
        self.assertEqual(rows["attendance"]["action"], "Edit configuration")
        self.assertEqual(rows["employee"]["configuration_status"], NOT_CONFIGURED)
        self.assertEqual(rows["employee"]["release_status"], "NOT RELEASED")
        self.assertEqual(rows["employee"]["action"], "Configure")
        self.assertEqual(rows["partial"]["configuration_status"], INCOMPLETE)
        self.assertEqual(rows["partial"]["action"], "Continue configuration")
        self.assertEqual(
            rows["attendance_view"]["configuration_status"], READ_ONLY
        )
        self.assertEqual(rows["attendance_view"]["release_status"], "—")
        self.assertEqual(rows["attendance_view"]["action"], "View")
        self.assertEqual(
            rows["attendance"]["action_url"],
            reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "attendance"},
            ),
        )
        self.assertEqual(
            rows["employee"]["action_url"],
            reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "employee"},
            ),
        )
        self.assertContains(response, reverse("admin:index"))
        self.assertContains(response, reverse("dbtool:table_list"))
        self.assertContains(
            response,
            reverse("admin:audit_operationlog_changelist"),
        )

    def test_normal_authenticated_user_is_forbidden(self):
        self.client.force_login(self.normal_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 403)


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
class ReleaseWorkflowTests(SemanticFixture, TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="root",
            email="root@example.com",
            password="password",
        )
        self.normal_user = get_user_model().objects.create_user(
            username="staff",
            password="password",
            is_staff=True,
        )
        self.introspector = StubIntrospector(
            self.attendance,
            self.employee,
        )
        self.release_url = reverse(
            "dbtool:table_release",
            kwargs={"schema": "public", "table": "attendance"},
        )
        self.unrelease_url = reverse(
            "dbtool:table_unrelease",
            kwargs={"schema": "public", "table": "attendance"},
        )

    def test_incomplete_table_cannot_be_released(self):
        profile = TableProfile.objects.create(
            schema_name="public",
            table_name="attendance",
            mutation_enabled=True,
        )
        self.client.force_login(self.superuser)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.post(self.release_url)

        self.assertEqual(response.status_code, 403)
        profile.refresh_from_db()
        self.assertFalse(profile.is_released)

    def test_review_explains_unreviewed_column(self):
        self.configure_attendance()
        ColumnProfile.objects.filter(column_name="note").update(reviewed=False)
        self.client.force_login(self.superuser)
        with patch("dbtool.views.SchemaIntrospector", return_value=self.introspector):
            response = self.client.get(reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "attendance"},
            ))
        self.assertContains(response, "note: Semantic metadata has not been reviewed.")
        self.assertNotContains(response, self.release_url)

    def test_complete_table_can_be_released_but_is_not_auto_released(self):
        self.configure_attendance()
        profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        self.assertEqual(
            self.service().resolve(self.attendance).configuration_status,
            COMPLETE,
        )
        self.assertFalse(profile.is_released)
        self.client.force_login(self.superuser)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.post(self.release_url)

        self.assertRedirects(
            response,
            reverse("dbtool:database_configuration"),
            fetch_redirect_response=False,
        )
        profile.refresh_from_db()
        self.assertTrue(profile.is_released)
        event = ConfigurationEvent.objects.get(action=ConfigurationEvent.Action.RELEASE)
        self.assertEqual(event.actor, self.superuser)
        self.assertEqual((event.was_released, event.is_released), (False, True))

    def test_mutations_disabled_blocks_release_even_when_complete(self):
        self.configure_attendance()
        TableProfile.objects.filter(table_name="attendance").update(
            mutation_enabled=False
        )
        self.client.force_login(self.superuser)
        with patch("dbtool.views.SchemaIntrospector", return_value=self.introspector):
            review = self.client.get(reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "attendance"},
            ))
            response = self.client.post(self.release_url)
        self.assertContains(review, "Enable mutations in Table configuration")
        self.assertNotContains(review, self.release_url)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(ConfigurationEvent.objects.exists())

    def test_release_rolls_back_if_event_cannot_be_written(self):
        self.configure_attendance()
        self.client.force_login(self.superuser)
        with (
            patch("dbtool.views.SchemaIntrospector", return_value=self.introspector),
            patch("dbtool.views.ConfigurationEvent.objects.create", side_effect=RuntimeError("log failed")),
        ):
            with self.assertRaisesMessage(RuntimeError, "log failed"):
                self.client.post(self.release_url)
        self.assertFalse(
            TableProfile.objects.get(table_name="attendance").is_released
        )

    def test_user_table_list_only_contains_released_tables(self):
        self.configure_attendance()
        profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        self.client.force_login(self.normal_user)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(reverse("dbtool:table_list"))

        self.assertNotContains(response, "public.attendance")
        self.assertEqual(response.context["tables"], [])

        profile.is_released = True
        profile.save(update_fields=["is_released"])
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(reverse("dbtool:table_list"))

        self.assertEqual(
            [item["name"] for item in response.context["tables"]],
            ["attendance"],
        )
        self.assertEqual(
            response.context["tables"][0]["action_url"],
            reverse(
                "dbtool:table_detail",
                kwargs={"schema": "public", "table": "attendance"},
            ),
        )
        self.assertContains(response, "Attendance")

    def test_normal_user_cannot_open_unreleased_table_directly(self):
        self.configure_attendance()
        self.client.force_login(self.normal_user)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(
                reverse(
                    "dbtool:table_detail",
                    kwargs={"schema": "public", "table": "attendance"},
                )
            )

        self.assertEqual(response.status_code, 403)

    def test_review_page_shows_release_state_and_matching_action(self):
        self.configure_attendance()
        profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        review_url = reverse(
            "dbtool:table_review",
            kwargs={"schema": "public", "table": "attendance"},
        )
        self.client.force_login(self.superuser)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(review_url)

        self.assertContains(response, "Configuration status:</strong> COMPLETE")
        self.assertContains(response, "Release status:</strong> NOT RELEASED")
        self.assertContains(response, self.release_url)
        self.assertNotContains(response, self.unrelease_url)

        profile.is_released = True
        profile.save(update_fields=["is_released"])
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(review_url)

        self.assertContains(response, "Release status:</strong> RELEASED")
        self.assertContains(response, self.unrelease_url)
        self.assertNotContains(response, self.release_url)

    def test_unrelease_hides_table_without_deleting_configuration(self):
        self.configure_attendance()
        profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        profile.is_released = True
        profile.save(update_fields=["is_released"])
        column_count = profile.column_profiles.count()
        lookup_count = LookupMapping.objects.filter(
            column_profile__table_profile=profile
        ).count()
        self.client.force_login(self.superuser)

        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.post(self.unrelease_url)

        self.assertRedirects(
            response,
            reverse("dbtool:database_configuration"),
            fetch_redirect_response=False,
        )
        profile.refresh_from_db()
        self.assertFalse(profile.is_released)
        event = ConfigurationEvent.objects.get(action=ConfigurationEvent.Action.UNRELEASE)
        self.assertEqual(event.actor, self.superuser)
        self.assertEqual((event.was_released, event.is_released), (True, False))
        self.assertEqual(profile.column_profiles.count(), column_count)
        self.assertEqual(
            LookupMapping.objects.filter(
                column_profile__table_profile=profile
            ).count(),
            lookup_count,
        )

        self.client.force_login(self.normal_user)
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(reverse("dbtool:table_list"))
        self.assertEqual(response.context["tables"], [])


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
class AutoUnreleaseTests(SemanticFixture, TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="root",
            email="root@example.com",
            password="password",
        )
        self.client.force_login(self.user)
        self.introspector = StubIntrospector(self.attendance, self.employee)
        self.configure_attendance()
        self.profile = TableProfile.objects.get(
            schema_name="public",
            table_name="attendance",
        )
        self.profile.is_released = True
        self.profile.save(update_fields=["is_released"])
        self.url = reverse(
            "dbtool:table_review",
            kwargs={"schema": "public", "table": "attendance"},
        )

    def review_data(self):
        return {
            "table-display_label": "Attendance",
            "table-mutation_enabled": "on",
            "column-0-reviewed": "on",
            "column-0-display_label": "",
            "column-0-allow_clear_on_update": "on",
            "column-0-input_mode": "text",
            "column-0-help_text": "",
            "column-1-reviewed": "on",
            "column-1-display_label": "Employee",
            "column-1-show_in_form": "on",
            "column-1-editable": "on",
            "column-1-required_on_insert": "on",
            "column-1-input_mode": "lookup",
            "column-1-help_text": "",
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

    def post_review(self, data):
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            return self.client.post(self.url, data)

    def assert_released(self):
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_released)

    def assert_unreleased(self):
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_released)

    def test_opening_configuration_does_not_unrelease(self):
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assert_released()

    def test_editing_without_save_does_not_unrelease(self):
        with patch(
            "dbtool.views.SchemaIntrospector",
            return_value=self.introspector,
        ):
            response = self.client.get(self.url)

        response.context["table_form"].initial["display_label"] = "Unsaved"
        self.assert_released()

    def test_no_op_save_does_not_unrelease(self):
        response = self.post_review(self.review_data())

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assert_released()
        self.assertFalse(ConfigurationEvent.objects.exists())

    def test_successful_table_profile_change_unreleases(self):
        data = self.review_data()
        data["table-display_label"] = "Attendance Events"

        response = self.post_review(data)

        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assert_unreleased()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.display_label, "Attendance Events")
        self.assertCountEqual(
            ConfigurationEvent.objects.values_list("action", flat=True),
            [ConfigurationEvent.Action.CONFIG_CHANGED,
             ConfigurationEvent.Action.AUTO_UNRELEASE],
        )
        self.assertEqual(
            ConfigurationEvent.objects.get(action=ConfigurationEvent.Action.CONFIG_CHANGED).actor,
            self.user,
        )

    def test_failed_save_does_not_unrelease_or_persist_changes(self):
        data = self.review_data()
        data["table-display_label"] = "Must Not Persist"
        data.pop("column-1-target_value_column")

        response = self.post_review(data)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["column_forms"][1][1].errors)
        self.assert_released()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.display_label, "Attendance")
        self.assertFalse(ConfigurationEvent.objects.exists())

    def test_persistence_failure_rolls_back_change_and_unrelease(self):
        data = self.review_data()
        data["table-display_label"] = "Must Roll Back"

        with patch(
            "dbtool.views.LookupMapping.objects.update_or_create",
            side_effect=RuntimeError("save failed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "save failed"):
                self.post_review(data)

        self.assert_released()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.display_label, "Attendance")
        self.assertFalse(ConfigurationEvent.objects.exists())

    def test_lookup_mapping_admin_change_unreleases(self):
        lookup = LookupMapping.objects.get(
            column_profile__table_profile=self.profile,
            column_profile__column_name="employee_code",
        )
        response = self.client.post(
            reverse("admin:dbtool_lookupmapping_change", args=[lookup.pk]),
            {
                "column_profile": lookup.column_profile_id,
                "target_schema": "public",
                "target_table": "employee",
                "target_value_column": "name",
                "display_columns": '["code", "name"]',
                "search_columns": '["code", "name"]',
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assert_unreleased()
        self.assertEqual(
            ConfigurationEvent.objects.filter(
                action=ConfigurationEvent.Action.AUTO_UNRELEASE
            ).count(), 1,
        )
        lookup.refresh_from_db()
        self.assertEqual(lookup.target_value_column, "name")

    def test_column_profile_admin_change_unreleases(self):
        column = self.profile.column_profiles.get(column_name="employee_code")
        response = self.client.post(
            reverse("admin:dbtool_columnprofile_change", args=[column.pk]),
            {
                "table_profile": self.profile.pk,
                "column_name": "employee_code",
                "display_label": "Employee",
                "show_in_form": "on",
                "editable": "on",
                "allow_clear_on_update": "",
                "input_mode": ColumnProfile.InputMode.LOOKUP,
                "help_text": "",
                "reviewed": "on",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assert_unreleased()
        column.refresh_from_db()
        self.assertFalse(column.required_on_insert)


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
            response = self.client.get(
                reverse("dbtool:database_configuration")
            )
            review_url = reverse(
                "dbtool:table_review",
                kwargs={"schema": "public", "table": "attendance"},
            )

        self.assertContains(response, review_url)
        self.assertContains(response, "Configure")

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


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class MultiOutputLookupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="mapping-user", password="password", is_staff=True
        )
        def column(name, data_type, *, primary_key=False, identity=False):
            return ColumnDefinition(
                name=name, data_type=data_type, nullable=False, default=None,
                primary_key=primary_key, identity=identity, generated=False,
            )
        cls.source = TableDefinition(
            schema="public", name="hr_employee", table_type="BASE TABLE",
            primary_key=["Id"],
            columns=[
                column("Id", "integer", primary_key=True, identity=True),
                column("EmployeeID", "text"),
                column("Name", "text"),
            ],
        )
        cls.destination = TableDefinition(
            schema="public", name="tracking", table_type="BASE TABLE",
            primary_key=["Id"],
            columns=[
                column("Id", "integer", primary_key=True, identity=True),
                column("EmployeeId", "integer"),
                column("Code", "text"),
                column("Name", "text"),
            ],
        )
        profile = TableProfile.objects.create(
            schema_name="public", table_name="tracking", mutation_enabled=True
        )
        for physical in cls.destination.columns:
            editable = physical.name != "Id"
            ColumnProfile.objects.create(
                table_profile=profile, column_name=physical.name,
                editable=editable, show_in_form=editable, reviewed=True,
                required_on_insert=editable,
                input_mode=(
                    ColumnProfile.InputMode.LOOKUP
                    if physical.name == "EmployeeId"
                    else ColumnProfile.InputMode.TEXT
                ),
            )
        selector = profile.column_profiles.get(column_name="EmployeeId")
        LookupMapping.objects.create(
            column_profile=selector, target_schema="public",
            target_table="hr_employee", target_value_column="Id",
            display_columns=["EmployeeID", "Name", "Id"],
            search_columns=["Name", "EmployeeID"],
            output_mappings=[
                {"source": "EmployeeID", "destination": "Code"},
                {"source": "Name", "destination": "Name"},
            ],
        )
        TableProfile.objects.filter(pk=profile.pk).update(is_released=True)

    def setUp(self):
        self.introspector = StubIntrospector(self.destination, self.source)
        self.effective = EffectiveMetadataService(
            introspector=self.introspector
        ).resolve(self.destination)
        self.service = MutationService()
        self.service.introspector = self.introspector
        self.service.metadata = EffectiveMetadataService(
            introspector=self.introspector
        )
        self.service.repository.introspector = self.introspector
        self.service.repository.lookup_value_exists = lambda *args: True

    def own(self, row):
        key = {"Id": row["Id"]}
        origin = OperationLog.objects.create(
            schema_name="public", table_name="tracking",
            action=OperationLog.Action.INSERT,
            status=OperationLog.Status.APPLIED,
            record_key=encode_row(key), after_data=encode_row(row), actor=self.user,
        )
        ToolOwnedRecord.objects.create(
            schema_name="public", table_name="tracking", key_digest=key_digest(key),
            record_key=encode_row(key), fingerprint=row_fingerprint(row),
            origin=origin, last_operation=origin,
        )

    def test_complete_release_and_form_only_asks_for_employee(self):
        self.assertEqual(self.effective.configuration_status, COMPLETE)
        self.assertTrue(self.effective.capabilities.can_insert)
        FormClass = build_row_form(self.effective, mode="insert")
        self.assertEqual(list(FormClass().fields), ["EmployeeId"])
        bound = FormClass({"EmployeeId": "28", "Code": "FAKE"})
        self.assertTrue(bound.is_valid())
        self.assertEqual(bound.cleaned_data, {"EmployeeId": 28})

    def test_insert_uses_selected_id_and_source_values_with_audit(self):
        source = {"Id": 28, "EmployeeID": "0028", "Name": "Same Name"}
        self.service.repository.get_row = lambda *args, **kwargs: source
        self.service.repository.insert_row = lambda schema, table, values: {
            "Id": 9, **values
        }
        with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
            after, operation = self.service.insert(
                schema="public", table="tracking",
                values={"EmployeeId": 28, "Code": "FAKE", "Name": "FAKE"},
                actor=self.user,
            )
        self.assertEqual(
            (after["EmployeeId"], after["Code"], after["Name"]),
            (28, "0028", "Same Name"),
        )
        self.assertEqual(operation.status, OperationLog.Status.APPLIED)

    def test_missing_or_incomplete_source_blocks_insert_without_audit(self):
        for source in (
            None,
            {"Id": 28, "EmployeeID": None, "Name": "A"},
            {"Id": 29, "EmployeeID": "029", "Name": "Wrong employee"},
        ):
            with self.subTest(source=source):
                self.service.repository.get_row = lambda *args, **kwargs: source
                with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
                    with self.assertRaises(MutationNotAllowed):
                        self.service.insert(
                            schema="public", table="tracking",
                            values={"EmployeeId": 28}, actor=self.user,
                        )
        self.assertFalse(OperationLog.objects.exists())

    def test_missing_source_on_changed_employee_blocks_update_without_audit(self):
        before = {"Id": 9, "EmployeeId": 28, "Code": "OLD", "Name": "Old"}
        self.service.repository.get_row = lambda schema, table, *args, **kwargs: (
            before if table == "tracking" else None
        )
        with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
            with self.assertRaises(MutationNotAllowed):
                self.service.update(
                    schema="public", table="tracking", record_key={"Id": 9},
                    values={"EmployeeId": 29},
                    expected_fingerprint=row_fingerprint(before), actor=self.user,
                )
        self.assertFalse(OperationLog.objects.exists())

    def test_unchanged_employee_keeps_existing_mapped_values_on_update(self):
        before = {"Id": 9, "EmployeeId": 28, "Code": "OLD", "Name": "Old"}
        self.own(before)
        self.service.repository.get_row = lambda *args, **kwargs: before
        self.service.repository.update_row = lambda schema, table, key, values: {
            **before, **values
        }
        with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
            after, _operation = self.service.update(
                schema="public", table="tracking", record_key={"Id": 9},
                values={"EmployeeId": 28, "Code": "FAKE"},
                expected_fingerprint=row_fingerprint(before), actor=self.user,
            )
        self.assertEqual((after["Code"], after["Name"]), ("OLD", "Old"))

    def test_changing_employee_updates_all_mapped_values(self):
        before = {"Id": 9, "EmployeeId": 28, "Code": "OLD", "Name": "Old"}
        self.own(before)
        source = {"Id": 29, "EmployeeID": "029", "Name": "New"}
        self.service.repository.get_row = lambda schema, table, *args, **kwargs: (
            before if table == "tracking" else source
        )
        self.service.repository.update_row = lambda schema, table, key, values: {
            **before, **values
        }
        with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
            after, _operation = self.service.update(
                schema="public", table="tracking", record_key={"Id": 9},
                values={"EmployeeId": 29, "Code": "FAKE"},
                expected_fingerprint=row_fingerprint(before), actor=self.user,
            )
        self.assertEqual(
            (after["EmployeeId"], after["Code"], after["Name"]),
            (29, "029", "New"),
        )

    def test_invalid_mapping_blocks_release_and_change_unreleases(self):
        mapping = LookupMapping.objects.get(
            column_profile__column_name="EmployeeId"
        )
        mapping.output_mappings = [
            {"source": "Missing", "destination": "Code"}
        ]
        mapping.save(update_fields=["output_mappings"])
        self.assertEqual(
            EffectiveMetadataService(introspector=self.introspector)
            .resolve(self.destination).configuration_status,
            INCOMPLETE,
        )
        mapping.column_profile.table_profile.refresh_from_db()
        self.assertFalse(mapping.column_profile.table_profile.is_released)

    def test_review_form_rejects_unknown_output_columns(self):
        catalog = {"public": {"hr_employee": ["Id", "EmployeeID", "Name"]}}
        form = ColumnReviewForm(
            {
                "input_mode": "lookup", "target_schema": "public",
                "target_table": "hr_employee", "target_value_column": "Id",
                "display_columns": ["Name"], "search_columns": ["Name"],
                "output_mappings": '[{"source":"Missing","destination":"Code"}]',
            },
            catalog=catalog,
            destination_columns=["EmployeeId", "Code", "Name"],
        )
        self.assertFalse(form.is_valid())
        self.assertIn("output_mappings", form.errors)

    def test_review_page_saves_mapping_and_actual_change_unreleases(self):
        root = get_user_model().objects.create_superuser(
            username="mapping-root", password="password"
        )
        self.client.force_login(root)
        url = reverse(
            "dbtool:table_review",
            kwargs={"schema": "public", "table": "tracking"},
        )
        data = {"table-display_label": "Tracking", "table-mutation_enabled": "on"}
        for index, name in enumerate(("Id", "EmployeeId", "Code", "Name")):
            prefix = f"column-{index}-"
            data[prefix + "reviewed"] = "on"
            data[prefix + "input_mode"] = "lookup" if name == "EmployeeId" else "text"
            if name != "Id":
                data[prefix + "show_in_form"] = "on"
                data[prefix + "editable"] = "on"
                data[prefix + "required_on_insert"] = "on"
        data.update({
            "column-1-target_schema": "public",
            "column-1-target_table": "hr_employee",
            "column-1-target_value_column": "Id",
            "column-1-display_columns": ["EmployeeID", "Name", "Id"],
            "column-1-search_columns": ["Name", "EmployeeID"],
            "column-1-output_mappings": '[{"source":"Name","destination":"Code"}]',
        })
        with patch("dbtool.views.SchemaIntrospector", return_value=self.introspector):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        lookup = LookupMapping.objects.get(column_profile__column_name="EmployeeId")
        self.assertEqual(
            lookup.output_mappings,
            [{"source": "Name", "destination": "Code"}],
        )
        lookup.column_profile.table_profile.refresh_from_db()
        self.assertFalse(lookup.column_profile.table_profile.is_released)

    def test_released_form_requires_employee_selection(self):
        self.client.force_login(self.user)
        with (
            patch("dbtool.views.SchemaIntrospector", return_value=self.introspector),
            patch("dbtool.views.MutationService") as mutation_class,
        ):
            response = self.client.post(
                reverse(
                    "dbtool:row_add",
                    kwargs={"schema": "public", "table": "tracking"},
                ),
                {},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("EmployeeId", response.context["form"].errors)
        mutation_class.assert_not_called()

    def test_stale_edit_does_not_read_source_or_mutate(self):
        before = {"Id": 9, "EmployeeId": 28, "Code": "OLD", "Name": "Old"}
        self.own(before)
        with patch.object(self.service.repository, "get_row", return_value=before) as get_row:
            with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
                from dbtool.mutations import StaleRowError
                with self.assertRaises(StaleRowError):
                    self.service.update(
                        schema="public", table="tracking", record_key={"Id": 9},
                        values={"EmployeeId": 29},
                        expected_fingerprint="stale", actor=self.user,
                    )
        get_row.assert_called_once()
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_legacy_row_cannot_be_updated_or_deleted_by_service(self):
        before = {"Id": 90, "EmployeeId": 28, "Code": "OLD", "Name": "Old"}
        with patch.object(self.service.repository, "get_row") as get_row:
            for action in (self.service.update, self.service.delete):
                with self.assertRaises(MutationNotAllowed):
                    action(schema="public", table="tracking", record_key={"Id": 90},
                           expected_fingerprint=row_fingerprint(before), actor=self.user,
                           **({"values": {"EmployeeId": 29}} if action == self.service.update else {}))
            get_row.assert_not_called()

    def test_legacy_row_direct_edit_and_delete_urls_are_hidden(self):
        self.client.force_login(self.user)
        token = make_row_token({"Id": 90})
        with (patch("dbtool.views.SchemaIntrospector", return_value=self.introspector),
              patch("dbtool.views.GenericTableRepository") as repository):
            for action in ("row_edit", "row_delete"):
                response = self.client.get(reverse(
                    f"dbtool:{action}", kwargs={"schema": "public", "table": "tracking", "row_token": token}
                ))
                self.assertEqual(response.status_code, 404)
            repository.return_value.get_row.assert_not_called()

    def test_owned_edit_page_opens_without_loading_lookup_rows(self):
        row = {"Id": 9, "EmployeeId": 28, "Code": "OWNED", "Name": "Own"}
        self.own(row)
        self.client.force_login(self.user)
        with (patch("dbtool.views.SchemaIntrospector", return_value=self.introspector),
              patch("dbtool.views.GenericTableRepository") as repository):
            repository.return_value.get_row.return_value = row
            response = self.client.get(reverse(
                "dbtool:row_edit", kwargs={"schema": "public", "table": "tracking",
                                              "row_token": make_row_token({"Id": 9})}
            ))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["form"].initial["EmployeeId"], 28)
            repository.return_value.search_lookup_rows.assert_not_called()

    def test_detail_lists_only_tool_owned_rows(self):
        self.client.force_login(self.user)
        row = {"Id": 9, "EmployeeId": 28, "Code": "OWNED", "Name": "Own"}
        self.own(row)
        with (patch("dbtool.views.SchemaIntrospector", return_value=self.introspector),
              patch("dbtool.views.GenericTableRepository") as repository):
            repository.return_value.rows_by_keys.return_value = [row]
            response = self.client.get(reverse(
                "dbtool:table_detail", kwargs={"schema": "public", "table": "tracking"}
            ))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["total_rows"], 1)
            repository.return_value.rows_by_keys.assert_called_once_with(self.destination, [{"Id": 9}])
            repository.return_value.list_rows.assert_not_called()
            repository.return_value.count_rows.assert_not_called()

    def test_insert_rollback_deactivates_owned_record(self):
        row = {"Id": 9, "EmployeeId": 28, "Code": "OWNED", "Name": "Own"}
        self.own(row)
        operation = OperationLog.objects.get(action=OperationLog.Action.INSERT)
        service = RollbackService()
        service.repository.get_row = lambda *args, **kwargs: row
        service.repository.delete_row = lambda *args, **kwargs: row
        with patch("dbtool.rollback.transaction.atomic", return_value=nullcontext()):
            service.rollback(operation)
        self.assertFalse(ToolOwnedRecord.objects.get(origin=operation).active)
        operation.refresh_from_db()
        self.assertEqual(operation.status, OperationLog.Status.ROLLED_BACK)

    def test_owned_delete_and_rollback_restore_visibility(self):
        row = {"Id": 9, "EmployeeId": 28, "Code": "OWNED", "Name": "Own"}
        self.own(row)
        self.service.repository.get_row = lambda *args, **kwargs: row
        self.service.repository.delete_row = lambda *args, **kwargs: row
        with patch("dbtool.mutations.transaction.atomic", return_value=nullcontext()):
            _deleted, operation = self.service.delete(
                schema="public", table="tracking", record_key={"Id": 9},
                expected_fingerprint=row_fingerprint(row), actor=self.user,
            )
        owned = ToolOwnedRecord.objects.get(schema_name="public", table_name="tracking")
        self.assertFalse(owned.active)
        self.assertEqual(owned.last_operation_id, operation.id)
        rollback = RollbackService()
        rollback.repository.get_row = lambda *args, **kwargs: None
        rollback.repository.restore_deleted_row = lambda *args, **kwargs: row
        with patch("dbtool.rollback.transaction.atomic", return_value=nullcontext()):
            rollback.rollback(operation)
        owned.refresh_from_db()
        self.assertTrue(owned.active)

    def test_legacy_operation_cannot_rollback(self):
        operation = OperationLog.objects.create(
            schema_name="public", table_name="tracking",
            action=OperationLog.Action.INSERT, status=OperationLog.Status.APPLIED,
            record_key=encode_row({"Id": 90}), actor=self.user,
        )
        with self.assertRaises(RollbackNotAllowed):
            RollbackService().rollback(operation)
        operation.refresh_from_db()
        self.assertEqual(operation.status, OperationLog.Status.APPLIED)
