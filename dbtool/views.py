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
from dbtool.models import ColumnProfile, LookupMapping, TableProfile
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

from audit.models import OperationLog
from dbtool.rollback import (
    RollbackNotAllowed,
    RollbackService,
)


PAGE_SIZE = 50


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
                    "Filled from lookup"
                    if column.capability.can_insert or column.capability.can_update
                    else "Read only"
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
    catalog = {}
    for schema, table, _table_type in introspector.list_tables():
        definition = introspector.inspect_table(schema, table)
        catalog.setdefault(schema, {})[table] = [
            column.name for column in definition.columns
        ]
    return catalog


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
        }

    initial = {
        "reviewed": profile.reviewed,
        "display_label": profile.display_label,
        "show_in_form": profile.show_in_form,
        "editable": profile.editable,
        "required_on_insert": profile.required_on_insert,
        "allow_clear_on_update": profile.allow_clear_on_update,
        "input_mode": profile.input_mode,
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


def table_list(request):
    introspector = SchemaIntrospector()
    metadata = EffectiveMetadataService(introspector=introspector)

    tables = []

    for schema, table_name, table_type in introspector.list_tables():
        definition = introspector.inspect_table(
            schema,
            table_name,
        )

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

    return render(
        request,
        "dbtool/table_list.html",
        {
            "tables": tables,
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

    for schema, table_name, _table_type in introspector.list_tables():
        definition = introspector.inspect_table(schema, table_name)
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
                    effective.configuration_status == COMPLETE and not released
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

    total_rows = repository.count_rows(
        schema,
        table,
    )

    total_pages = max(
        math.ceil(total_rows / PAGE_SIZE),
        1,
    )

    if page > total_pages:
        page = total_pages

    offset = (page - 1) * PAGE_SIZE

    raw_rows = repository.list_rows(
        schema,
        table,
        limit=PAGE_SIZE,
        offset=offset,
    )

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

            status = EffectiveMetadataService(
                introspector=introspector
            ).resolve(definition).status
            messages.success(request, f"Configuration saved. Status: {status}.")
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
                effective.configuration_status == COMPLETE
                and effective.profile is not None
                and not effective.profile.is_released
            ),
            "can_unrelease": bool(
                effective.profile and effective.profile.is_released
            ),
            "table_form": table_form,
            "column_forms": column_forms,
            "catalog": catalog,
            "definition_columns": [column.name for column in definition.columns],
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

    effective = EffectiveMetadataService(introspector=introspector).resolve(
        definition
    )
    if (
        effective.configuration_status != COMPLETE
        or effective.profile is None
        or effective.profile.is_released
    ):
        raise PermissionDenied(
            "Only complete, unreleased table configuration can be released."
        )

    effective.profile.is_released = True
    effective.profile.save(update_fields=["is_released"])
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

    profile = get_object_or_404(
        TableProfile,
        schema_name=schema,
        table_name=table,
    )
    if not profile.is_released:
        raise PermissionDenied("Table is not released.")

    profile.is_released = False
    profile.save(update_fields=["is_released"])
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

    current = repository.get_row(
        schema,
        table,
        record_key,
    )

    if current is None:
        raise Http404("Row does not exist.")

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

    current = repository.get_row(
        schema,
        table,
        record_key,
    )

    if current is None:
        raise Http404("Row does not exist.")

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
