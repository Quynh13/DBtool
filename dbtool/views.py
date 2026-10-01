import math

from django.contrib import messages
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render

from dbtool.forms import build_row_form
from dbtool.introspection import SchemaIntrospector
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
from django.shortcuts import get_object_or_404
from django.urls import reverse

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


def _safety_label(definition, capabilities):
    if definition.table_type != "BASE TABLE":
        return "READ ONLY"

    if capabilities.can_insert and capabilities.can_update and capabilities.can_delete:
        return "SAFE"

    return "RESTRICTED"


def _load_record_key(row_token):
    try:
        return load_row_token(row_token)
    except signing.BadSignature as exc:
        raise Http404("Invalid row identifier.") from exc


def table_list(request):
    introspector = SchemaIntrospector()
    policy = SafetyPolicy()

    tables = []

    for schema, table_name, table_type in introspector.list_tables():
        definition = introspector.inspect_table(
            schema,
            table_name,
        )

        capabilities = policy.evaluate(definition)

        tables.append(
            {
                "schema": schema,
                "name": table_name,
                "type": table_type,
                "primary_key": definition.primary_key,
                "safety": _safety_label(
                    definition,
                    capabilities,
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


def table_detail(request, schema, table):
    introspector = SchemaIntrospector()
    policy = SafetyPolicy()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    capabilities = policy.evaluate(definition)

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
            "capabilities": capabilities,
            "column_rows": column_rows,
            "safety": _safety_label(
                definition,
                capabilities,
            ),
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


def row_add(request, schema, table):
    introspector = SchemaIntrospector()
    policy = SafetyPolicy()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    capabilities = policy.evaluate(definition)

    if not capabilities.can_insert:
        raise PermissionDenied("INSERT is not allowed for this table.")

    FormClass = build_row_form(
        definition,
        capabilities,
        mode="insert",
    )

    if request.method == "POST":
        form = FormClass(request.POST)

        if form.is_valid():
            values = {}

            for column in definition.columns:
                capability = capabilities.columns[column.name]

                if not capability.can_insert:
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

    return render(
        request,
        "dbtool/row_form.html",
        {
            "definition": definition,
            "form": form,
            "mode": "Add",
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
    policy = SafetyPolicy()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    capabilities = policy.evaluate(definition)

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
        definition,
        capabilities,
        mode="update",
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
        }

        form = FormClass(initial=initial)

        state_token = make_state_token(
            record_key,
            current,
        )

    return render(
        request,
        "dbtool/row_form.html",
        {
            "definition": definition,
            "form": form,
            "mode": "Edit",
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
    policy = SafetyPolicy()
    repository = GenericTableRepository()

    try:
        definition = introspector.inspect_table(
            schema,
            table,
        )
    except ValueError as exc:
        raise Http404(str(exc)) from exc

    capabilities = policy.evaluate(definition)

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
