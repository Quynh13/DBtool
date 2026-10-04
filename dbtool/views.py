import math

from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render

from dbtool.forms import (
    ColumnReviewForm,
    TableReviewForm,
    build_row_form,
    populate_lookup_choices,
)
from dbtool.introspection import SchemaIntrospector
from dbtool.models import (
    COLUMN_SEMANTIC_FIELDS,
    LOOKUP_SEMANTIC_FIELDS,
    TABLE_SEMANTIC_FIELDS,
    ColumnProfile,
    ConfigurationEvent,
    LookupMapping,
    TableProfile,
)
from dbtool.mutations import (
    MutationService,
    RowNotFound,
    StaleRowError,
)
from dbtool.repository import GenericTableRepository
from dbtool.row_state import (
    load_row_token,
    load_state_token,
    make_row_token,
    make_state_token,
)
from dbtool.safety import SafetyPolicy
from dbtool.semantics import (
    COMPLETE,
    INCOMPLETE,
    NEEDS_REVIEW,
    NOT_CONFIGURED,
    READY,
    READ_ONLY,
    EffectiveMetadataService,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST

from audit.models import OperationLog, ToolOwnedRecord
from audit.codec import decode_row
from dbtool.ownership import key_digest, owned_record, verified_owned_record
from dbtool.row_state import row_fingerprint
from dbtool.rollback import (
    RollbackNotAllowed,
    RollbackService,
)


PAGE_SIZE = 50


def _can_release(effective):
    return (
        effective.configuration_status == COMPLETE
        and effective.status == READY
        and effective.profile is not None
        and not effective.profile.is_released
    )


def _release_blockers(effective):
    reasons = list(effective.warnings)
    if effective.profile and not effective.profile.mutation_enabled:
        reasons.append("Enable mutations in Table configuration before Release.")
    return reasons


def _semantic_snapshot(profile):
    if profile is None:
        return None
    columns = []
    for column in profile.column_profiles.all():
        try:
            lookup = column.lookup_mapping
        except LookupMapping.DoesNotExist:
            lookup = None
        columns.append((
            tuple(getattr(column, field) for field in COLUMN_SEMANTIC_FIELDS),
            tuple(getattr(lookup, field) for field in LOOKUP_SEMANTIC_FIELDS)
            if lookup else None,
        ))
    return (
        tuple(getattr(profile, field) for field in TABLE_SEMANTIC_FIELDS),
        tuple(sorted(columns, key=lambda item: item[0][1])),
    )


def _display_value(value):
    if value is None:
        return "NULL"

    if isinstance(value, memoryview):
        return f"<binary: {len(value)} bytes>"

    if isinstance(value, bytes):
        return f"<binary: {len(value)} bytes>"

    return str(value)


def _load_record_key(row_token):
    try:
        return load_row_token(row_token)
    except signing.BadSignature as exc:
        raise Http404("Invalid row identifier.") from exc


def _lookup_urls(effective):
    return {
        name: reverse(
            "dbtool:lookup_search",
            kwargs={
                "schema": effective.definition.schema,
                "table": effective.definition.name,
                "column": name,
            },
        )
        for name, column in effective.columns.items()
        if column.lookup is not None
    }


def _form_sections(form, effective, mode):
    required = []
    optional = []
    for name in form.fields:
        column = effective.columns[name]
        is_required = (
            column.required_on_insert
            if mode == "insert"
            else not column.allow_clear_on_update
        )
        item = {"field": form[name], "required": is_required}
        (required if is_required else optional).append(item)

    system = [
        {
            "label": column.label,
            "help_text": column.help_text,
            "reason": (
                "Database managed"
                if column.physical.identity or column.physical.generated
                else (
                    "Fixed on insert"
                    if column.input_mode == ColumnProfile.InputMode.FIXED
                    else (
                        "Filled from lookup"
                        if column.capability.can_insert or column.capability.can_update
                        else "Read only"
                    )
                )
            ),
        }
        for name, column in effective.columns.items()
        if name not in form.fields
    ]
    return (
        ("Required", required),
        ("Optional", optional),
        ("System managed", system),
    )


def _metadata_catalog(introspector):
    return introspector.column_catalog()


def _column_review_initial(column, profile, capability):
    if profile is None:
        editable = capability.can_insert or capability.can_update
        return {
            "reviewed": False,
            "display_label": column.name,
            "show_in_form": editable,
            "editable": editable,
            "required_on_insert": capability.required_on_insert,
            "allow_clear_on_update": column.nullable,
            "input_mode": ColumnProfile.InputMode.TEXT,
            "fixed_insert_value": "",
        }

    initial = {
        "reviewed": profile.reviewed,
        "display_label": profile.display_label,
        "show_in_form": profile.show_in_form,
        "editable": profile.editable,
        "required_on_insert": profile.required_on_insert,
        "allow_clear_on_update": profile.allow_clear_on_update,
        "input_mode": profile.input_mode,
        "fixed_insert_value": profile.fixed_insert_value,
        "help_text": profile.help_text,
    }
    try:
        lookup = profile.lookup_mapping
    except LookupMapping.DoesNotExist:
        return initial
    initial.update(
        {
            "target_schema": lookup.target_schema,
            "target_table": lookup.target_table,
            "target_value_column": lookup.target_value_column,
            "display_columns": lookup.display_columns,
            "search_columns": lookup.search_columns,
            "output_mappings": lookup.output_mappings,
            "allow_null": lookup.allow_null,
            "allow_arbitrary_value": lookup.allow_arbitrary_value,
        }
    )
    return initial


def released_tables():
    introspector = SchemaIntrospector()
    metadata = EffectiveMetadataService(introspector=introspector)

    tables = []

    for profile in TableProfile.objects.filter(is_released=True):
        schema, table_name = profile.schema_name, profile.table_name
        try:
            definition = introspector.inspect_table(schema, table_name)
        except ValueError:
            continue
        table_type = definition.table_type

        effective = metadata.resolve(definition)

        if not effective.profile or not effective.profile.is_released:
            continue

        tables.append(
            {
                "schema": schema,
                "name": table_name,
                "type": table_type,
                "primary_key": definition.primary_key,
                "label": effective.display_label,
                "safety": effective.status,
                "action": (
                    "Open" if effective.status == READY else "View"
                ),
                "action_url": reverse(
                    "dbtool:table_detail",
                    kwargs={"schema": schema, "table": table_name},
                ),
            }
        )

    return tables


def table_list(request):
    return render(
        request,
        "dbtool/table_list.html",
        {
            "tables": released_tables(),
        },
    )


def database_configuration(request):
    if not request.user.is_superuser:
        raise PermissionDenied(
            "Only a superuser can access database configuration."
        )

    introspector = SchemaIntrospector()
    metadata = EffectiveMetadataService(introspector=introspector)
    actions = {
        NOT_CONFIGURED: "Configure",
        INCOMPLETE: "Continue configuration",
        COMPLETE: "Edit configuration",
        READ_ONLY: "View",
    }
    tables = []

    for definition in introspector.list_definitions():
        schema, table_name = definition.schema, definition.name
        effective = metadata.resolve(definition)
        released = bool(effective.profile and effective.profile.is_released)
        tables.append(
            {
                "schema": schema,
                "name": table_name,
                "configuration_status": effective.configuration_status,
                "release_status": (
                    "RELEASED"
                    if released
                    else (
                        "—"
                        if effective.configuration_status == READ_ONLY
                        else "NOT RELEASED"
                    )
                ),
                "action": actions[effective.configuration_status],
                "action_url": reverse(
                    "dbtool:table_review",
                    kwargs={"schema": schema, "table": table_name},
                ),
                "can_release": (
                    _can_release(effective)
                ),
                "can_unrelease": released,
                "release_url": reverse(
                    "dbtool:table_release",
                    kwargs={"schema": schema, "table": table_name},
                ),
                "unrelease_url": reverse(
                    "dbtool:table_unrelease",
                    kwargs={"schema": schema, "table": table_name},
                ),
            }
        )

    return render(
        request,
        "dbtool/database_configuration.html",
        {"tables": tables},
    )


def table_detail(request, schema, table):
    introspector = SchemaIntrospector()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    if (
        not request.user.is_superuser
        and (not effective.profile or not effective.profile.is_released)
    ):
        raise PermissionDenied("This table has not been released.")
    capabilities = effective.capabilities

    column_rows = []

    for column in definition.columns:
        capability = capabilities.columns[column.name]

        column_rows.append(
            {
                "column": column,
                "nullable_display": ("Yes" if column.nullable else "No"),
                "pk_display": ("Yes" if column.primary_key else "—"),
                "insert_display": ("✓" if capability.can_insert else "—"),
                "update_display": ("✓" if capability.can_update else "—"),
                "warning_display": ", ".join(capability.warnings),
                "label": effective.columns[column.name].label,
                "group": (
                    "System managed"
                    if effective.columns[column.name].system_managed
                    else (
                        "Required"
                        if effective.columns[column.name].required_on_insert
                        else "Optional"
                    )
                ),
            }
        )

    try:
        page = int(
            request.GET.get(
                "page",
                "1",
            )
        )
    except ValueError:
        page = 1

    page = max(page, 1)

    owned = ToolOwnedRecord.objects.filter(
        schema_name=schema, table_name=table, active=True
    ).order_by("pk")
    total_rows = owned.count()

    total_pages = max(
        math.ceil(total_rows / PAGE_SIZE),
        1,
    )

    if page > total_pages:
        page = total_pages

    offset = (page - 1) * PAGE_SIZE

    owned_page = list(owned[offset:offset + PAGE_SIZE])
    raw_rows = repository.rows_by_keys(
        definition, [decode_row(item.record_key) for item in owned_page]
    )
    fingerprints = {
        item.key_digest: item.fingerprint for item in owned_page
    }
    raw_rows = [
        row for row in raw_rows
        if fingerprints.get(key_digest({name: row[name] for name in definition.primary_key}))
        == row_fingerprint(row)
    ]

    rows = []

    for row in raw_rows:
        row_token = None

        if definition.primary_key:
            record_key = {name: row[name] for name in definition.primary_key}

            row_token = make_row_token(record_key)

        rows.append(
            {
                "values": [
                    _display_value(row.get(column.name))
                    for column in definition.columns
                ],
                "row_token": row_token,
            }
        )

    primary_key_display = (
        ", ".join(definition.primary_key) if definition.primary_key else "None"
    )

    return render(
        request,
        "dbtool/table_detail.html",
        {
            "definition": definition,
            "display_label": effective.display_label,
            "capabilities": capabilities,
            "column_rows": column_rows,
            "safety": effective.status,
            "configuration_action": (
                "Review configuration"
                if effective.status == NEEDS_REVIEW
                else "Edit configuration"
            ),
            "show_configuration_action": effective.status in {
                NEEDS_REVIEW,
                READY,
            },
            "rows": rows,
            "total_rows": total_rows,
            "page": page,
            "total_pages": total_pages,
            "has_previous": page > 1,
            "has_next": page < total_pages,
            "previous_page": page - 1,
            "next_page": page + 1,
            "primary_key_display": (primary_key_display),
            "column_count": max(
                len(definition.columns) + 1,
                1,
            ),
        },
    )


def table_review(request, schema, table):
    if not request.user.is_superuser:
        raise PermissionDenied(
            "Only a superuser can review table configuration."
        )

    introspector = SchemaIntrospector()
    try:
        definition = introspector.inspect_table(schema, table)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    profile = (
        TableProfile.objects.filter(schema_name=schema, table_name=table)
        .prefetch_related("column_profiles__lookup_mapping")
        .first()
    )
    existing_profiles = (
        {item.column_name: item for item in profile.column_profiles.all()}
        if profile
        else {}
    )
    previous_semantics = _semantic_snapshot(profile)
    was_released = bool(profile and profile.is_released)
    catalog = _metadata_catalog(introspector)
    physical = SafetyPolicy().evaluate(definition)
    table_initial = {
        "display_label": profile.display_label if profile else definition.name,
        "mutation_enabled": profile.mutation_enabled if profile else True,
    }
    table_form = TableReviewForm(
        request.POST or None,
        prefix="table",
        initial=table_initial,
    )
    column_forms = []
    for index, column in enumerate(definition.columns):
        column_profile = existing_profiles.get(column.name)
        column_forms.append(
            (
                column,
                ColumnReviewForm(
                    request.POST or None,
                    prefix=f"column-{index}",
                    catalog=catalog,
                    destination_columns=[item.name for item in definition.columns],
                    physical_column=column,
                    initial=_column_review_initial(
                        column,
                        column_profile,
                        physical.columns[column.name],
                    ),
                ),
            )
        )

    if request.method == "POST":
        table_valid = table_form.is_valid()
        columns_valid = all([form.is_valid() for _column, form in column_forms])
        if table_valid and columns_valid:
            with transaction.atomic():
                profile, _created = TableProfile.objects.update_or_create(
                    schema_name=schema,
                    table_name=table,
                    defaults=table_form.cleaned_data,
                )
                physical_names = {column.name for column in definition.columns}
                profile.column_profiles.exclude(
                    column_name__in=physical_names
                ).delete()

                for column, form in column_forms:
                    data = form.cleaned_data
                    column_profile, _created = (
                        ColumnProfile.objects.update_or_create(
                            table_profile=profile,
                            column_name=column.name,
                            defaults={
                                "reviewed": data["reviewed"],
                                "display_label": data["display_label"],
                                "show_in_form": data["show_in_form"],
                                "editable": data["editable"],
                                "required_on_insert": data[
                                    "required_on_insert"
                                ],
                                "allow_clear_on_update": data[
                                    "allow_clear_on_update"
                                ],
                                "input_mode": data["input_mode"],
                                "fixed_insert_value": data["fixed_insert_value"],
                                "help_text": data["help_text"],
                            },
                        )
                    )
                    if data["input_mode"] == ColumnProfile.InputMode.LOOKUP:
                        LookupMapping.objects.update_or_create(
                            column_profile=column_profile,
                            defaults={
                                "target_schema": data["target_schema"],
                                "target_table": data["target_table"],
                                "target_value_column": data[
                                    "target_value_column"
                                ],
                                "display_columns": data["display_columns"],
                                "search_columns": data["search_columns"],
                                "output_mappings": data["output_mappings"],
                                "allow_null": data["allow_null"],
                                "allow_arbitrary_value": data[
                                    "allow_arbitrary_value"
                                ],
                            },
                        )
                    else:
                        LookupMapping.objects.filter(
                            column_profile=column_profile
                        ).delete()

                saved_profile = (
                    TableProfile.objects.filter(pk=profile.pk)
                    .prefetch_related("column_profiles__lookup_mapping")
                    .get()
                )
                if _semantic_snapshot(saved_profile) != previous_semantics:
                    ConfigurationEvent.objects.create(
                        schema_name=schema,
                        table_name=table,
                        action=ConfigurationEvent.Action.CONFIG_CHANGED,
                        actor=request.user,
                        was_released=was_released,
                        is_released=saved_profile.is_released,
                    )

            status = EffectiveMetadataService(
                introspector=introspector
            ).resolve(definition).configuration_status
            messages.success(request, f"Configuration saved. Status: {status}. Release separately when ready.")
            return redirect(
                "dbtool:table_review",
                schema=schema,
                table=table,
            )

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    return render(
        request,
        "dbtool/table_review.html",
        {
            "definition": definition,
            "status": effective.status,
            "configuration_status": effective.configuration_status,
            "release_status": (
                "RELEASED"
                if effective.profile and effective.profile.is_released
                else "NOT RELEASED"
            ),
            "can_release": (
                _can_release(effective)
            ),
            "can_unrelease": bool(
                effective.profile and effective.profile.is_released
            ),
            "table_form": table_form,
            "column_forms": column_forms,
            "catalog": catalog,
            "definition_columns": [column.name for column in definition.columns],
            "release_blockers": _release_blockers(effective),
        },
    )


@require_POST
def table_release(request, schema, table):
    if not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can release tables.")

    introspector = SchemaIntrospector()
    try:
        definition = introspector.inspect_table(schema, table)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    with transaction.atomic():
        effective = EffectiveMetadataService(introspector=introspector).resolve(
            definition
        )
        if not _can_release(effective):
            raise PermissionDenied(
                "Only complete, mutation-enabled, unreleased table configuration can be released."
            )
        effective.profile.is_released = True
        effective.profile.save(update_fields=["is_released"])
        ConfigurationEvent.objects.create(
            schema_name=schema,
            table_name=table,
            action=ConfigurationEvent.Action.RELEASE,
            actor=request.user,
            was_released=False,
            is_released=True,
        )
    messages.success(request, f"{schema}.{table} released.")
    return redirect("dbtool:database_configuration")


@require_POST
def table_unrelease(request, schema, table):
    if not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can unrelease tables.")

    try:
        SchemaIntrospector().inspect_table(schema, table)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    with transaction.atomic():
        profile = get_object_or_404(
            TableProfile,
            schema_name=schema,
            table_name=table,
        )
        if not profile.is_released:
            raise PermissionDenied("Table is not released.")
        profile.is_released = False
        profile.save(update_fields=["is_released"])
        ConfigurationEvent.objects.create(
            schema_name=schema,
            table_name=table,
            action=ConfigurationEvent.Action.UNRELEASE,
            actor=request.user,
            was_released=True,
            is_released=False,
        )
    messages.success(request, f"{schema}.{table} unreleased.")
    return redirect("dbtool:database_configuration")


def row_add(request, schema, table):
    introspector = SchemaIntrospector()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    capabilities = effective.capabilities

    if not capabilities.can_insert:
        raise PermissionDenied("INSERT is not allowed for this table.")

    FormClass = build_row_form(
        effective,
        mode="insert",
        lookup_urls=_lookup_urls(effective),
    )

    if request.method == "POST":
        form = FormClass(request.POST)

        if form.is_valid():
            values = {}

            for column in definition.columns:
                capability = capabilities.columns[column.name]

                if not capability.can_insert or column.name not in form.fields:
                    continue

                value = form.cleaned_data[column.name]

                raw_value = request.POST.get(
                    column.name,
                    "",
                )

                if raw_value == "" and column.default is not None:
                    continue

                values[column.name] = value

            try:
                MutationService().insert(
                    schema=schema,
                    table=table,
                    values=values,
                    actor=request.user,
                )

            except Exception as exc:
                form.add_error(
                    None,
                    str(exc),
                )

            else:
                messages.success(
                    request,
                    "Row inserted successfully.",
                )

                return redirect(
                    "dbtool:table_detail",
                    schema=schema,
                    table=table,
                )

    else:
        form = FormClass()

    populate_lookup_choices(form, effective)

    return render(
        request,
        "dbtool/row_form.html",
        {
            "definition": definition,
            "form": form,
            "mode": "Add",
            "display_label": effective.display_label,
            "form_sections": _form_sections(form, effective, "insert"),
            "state_token": "",
        },
    )


def row_edit(
    request,
    schema,
    table,
    row_token,
):
    introspector = SchemaIntrospector()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    capabilities = effective.capabilities

    if not capabilities.can_update:
        raise PermissionDenied("UPDATE is not allowed for this table.")

    record_key = _load_record_key(row_token)

    if owned_record(schema, table, record_key) is None:
        raise Http404("Row is not managed by this tool.")

    current = repository.get_row(
        schema,
        table,
        record_key,
    )

    if current is None:
        raise Http404("Row does not exist.")
    if verified_owned_record(schema, table, record_key, current) is None:
        raise Http404("Row is not managed by this tool.")

    FormClass = build_row_form(
        effective,
        mode="update",
        lookup_urls=_lookup_urls(effective),
    )

    if request.method == "POST":
        form = FormClass(request.POST)

        if form.is_valid():
            try:
                state_key, fingerprint = load_state_token(
                    request.POST.get(
                        "state_token",
                        "",
                    )
                )

            except signing.BadSignature:
                raise PermissionDenied("Invalid row state token.")

            if state_key != record_key:
                raise PermissionDenied("Row state does not match row identifier.")

            values = {
                column.name: form.cleaned_data[column.name]
                for column in definition.columns
                if capabilities.columns[column.name].can_update
                and column.name in form.fields
            }

            try:
                MutationService().update(
                    schema=schema,
                    table=table,
                    record_key=record_key,
                    values=values,
                    expected_fingerprint=fingerprint,
                    actor=request.user,
                )

            except StaleRowError:
                messages.error(
                    request,
                    "Row changed after this form was opened. "
                    "The latest values have been reloaded.",
                )

                return redirect(
                    "dbtool:row_edit",
                    schema=schema,
                    table=table,
                    row_token=row_token,
                )

            except RowNotFound as exc:
                raise Http404(str(exc)) from exc

            except Exception as exc:
                form.add_error(
                    None,
                    str(exc),
                )

            else:
                messages.success(
                    request,
                    "Row updated successfully.",
                )

                return redirect(
                    "dbtool:table_detail",
                    schema=schema,
                    table=table,
                )

        state_token = request.POST.get(
            "state_token",
            "",
        )

    else:
        form = FormClass()
        initial = {
            column.name: current[column.name]
            for column in definition.columns
            if capabilities.columns[column.name].can_update
            and column.name in form.fields
        }

        form = FormClass(initial=initial)

        state_token = make_state_token(
            record_key,
            current,
        )

    populate_lookup_choices(form, effective, current)

    return render(
        request,
        "dbtool/row_form.html",
        {
            "definition": definition,
            "form": form,
            "mode": "Edit",
            "display_label": effective.display_label,
            "form_sections": _form_sections(form, effective, "update"),
            "state_token": state_token,
        },
    )


def row_delete(
    request,
    schema,
    table,
    row_token,
):
    introspector = SchemaIntrospector()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    capabilities = effective.capabilities

    if not capabilities.can_delete:
        raise PermissionDenied("DELETE is not allowed for this table.")

    record_key = _load_record_key(row_token)

    if owned_record(schema, table, record_key) is None:
        raise Http404("Row is not managed by this tool.")

    current = repository.get_row(
        schema,
        table,
        record_key,
    )

    if current is None:
        raise Http404("Row does not exist.")
    if verified_owned_record(schema, table, record_key, current) is None:
        raise Http404("Row is not managed by this tool.")

    if request.method == "POST":
        try:
            state_key, fingerprint = load_state_token(
                request.POST.get(
                    "state_token",
                    "",
                )
            )

        except signing.BadSignature:
            raise PermissionDenied("Invalid row state token.")

        if state_key != record_key:
            raise PermissionDenied("Row state does not match row identifier.")

        try:
            MutationService().delete(
                schema=schema,
                table=table,
                record_key=record_key,
                expected_fingerprint=fingerprint,
                actor=request.user,
            )

        except StaleRowError:
            messages.error(
                request,
                "Row changed after the delete page was opened. Delete was blocked.",
            )

            return redirect(
                "dbtool:table_detail",
                schema=schema,
                table=table,
            )

        except RowNotFound as exc:
            raise Http404(str(exc)) from exc

        else:
            messages.success(
                request,
                "Row deleted successfully.",
            )

            return redirect(
                "dbtool:table_detail",
                schema=schema,
                table=table,
            )

    state_token = make_state_token(
        record_key,
        current,
    )

    display_values = [
        {
            "name": column.name,
            "value": _display_value(current.get(column.name)),
        }
        for column in definition.columns
    ]

    return render(
        request,
        "dbtool/row_delete.html",
        {
            "definition": definition,
            "display_values": display_values,
            "state_token": state_token,
        },
    )


def lookup_search(request, schema, table, column):
    introspector = SchemaIntrospector()
    try:
        definition = introspector.inspect_table(schema, table)
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    effective_column = effective.columns.get(column)
    if (
        effective.status != READY
        or not effective.profile
        or not effective.profile.is_released
        or not effective_column
        or not effective_column.lookup
    ):
        raise Http404("Lookup is not configured.")

    lookup = effective_column.lookup
    results = GenericTableRepository().search_lookup_rows(
        lookup.target_schema,
        lookup.target_table,
        value_column=lookup.target_value_column,
        display_columns=lookup.display_columns,
        search_columns=lookup.search_columns,
        query=request.GET.get("q", "").strip()[:100],
    )
    return JsonResponse({"results": results})


def operation_rollback(
    request,
    operation_id,
):
    operation = get_object_or_404(
        OperationLog,
        pk=operation_id,
    )

    allowed = operation.status in {
        OperationLog.Status.APPLIED,
        OperationLog.Status.ROLLBACK_FAILED,
    }

    if not allowed:
        raise PermissionDenied("This operation cannot be rolled back.")

    if request.method == "POST":
        try:
            RollbackService().rollback(operation)

        except Exception as exc:
            messages.error(
                request,
                f"Rollback blocked: {exc}",
            )

        else:
            messages.success(
                request,
                "Rollback completed successfully.",
            )

        return redirect(reverse("admin:audit_operationlog_changelist"))

    return render(
        request,
        "dbtool/rollback_confirm.html",
        {
            "operation": operation,
        },
    )
