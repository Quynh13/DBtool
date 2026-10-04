import json
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from audit.models import OperationLog
from dbtool.forms import build_row_form
from dbtool.introspection import SchemaIntrospector
from dbtool.models import ColumnProfile, ConfigurationEvent
from dbtool.mutations import MutationService, RowNotFound, StaleRowError
from dbtool.ownership import owned_record, verified_owned_record
from dbtool.repository import GenericTableRepository
from dbtool.row_state import load_row_token, load_state_token, make_state_token
from dbtool.rollback import RollbackService
from dbtool.semantics import EffectiveMetadataService
from dbtool.views import (
    _display_value,
    lookup_search as legacy_lookup_search,
    released_tables,
    table_detail_context,
    database_configuration_tables,
    table_review as legacy_table_review,
    table_release as legacy_table_release,
    table_unrelease as legacy_table_unrelease,
)


@require_GET
@ensure_csrf_cookie
def frontend(request):
    return render(request, "index.html")


@require_http_methods(["GET", "POST", "DELETE"])
def session(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            return JsonResponse({"error": "Invalid request."}, status=400)
        if not isinstance(data, dict):
            return JsonResponse({"error": "Invalid request."}, status=400)
        user = authenticate(
            request, username=data.get("username"), password=data.get("password")
        )
        if user is None or not user.is_staff:
            return JsonResponse({"error": "Invalid credentials."}, status=400)
        login(request, user)
    elif request.method == "DELETE":
        logout(request)
    user = request.user
    return JsonResponse({
        "authenticated": bool(user.is_authenticated and user.is_active and user.is_staff),
        "username": user.get_username() if user.is_authenticated else None,
        "is_superuser": bool(user.is_authenticated and user.is_superuser),
    })


def staff_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Login required."}, status=401)
        if not request.user.is_active or not request.user.is_staff:
            return JsonResponse({"error": "Staff access required."}, status=403)
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied as exc:
            return JsonResponse({"error": str(exc)}, status=403)
        except Http404 as exc:
            return JsonResponse({"error": str(exc)}, status=404)
    return wrapped


@require_GET
@staff_required
def table_list(request):
    return JsonResponse({"tables": released_tables()})


@require_GET
@staff_required
def table_detail(request, schema, table):
    context = table_detail_context(request, schema, table)
    definition = context["definition"]
    capabilities = context["capabilities"]
    return JsonResponse({
        "schema": schema,
        "table": table,
        "label": context["display_label"],
        "status": context["safety"],
        "columns": [
            {"name": column.name, "label": row["label"]}
            for column, row in zip(definition.columns, context["column_rows"])
        ],
        "rows": context["rows"],
        "page": context["page"],
        "total_pages": context["total_pages"],
        "total_rows": context["total_rows"],
        "can_insert": capabilities.can_insert,
        "can_update": capabilities.can_update,
        "can_delete": capabilities.can_delete,
    })


def _table(request, schema, table):
    introspector = SchemaIntrospector()
    try:
        definition = introspector.inspect_table(schema, table)
    except ValueError:
        return None, None
    effective = EffectiveMetadataService(introspector=introspector).resolve(definition)
    if not request.user.is_superuser and (
        not effective.profile or not effective.profile.is_released
    ):
        return definition, None
    return definition, effective


def _row_form_data(form, effective, state_token=""):
    return {
        "fields": [
            {
                "name": name,
                "label": field.label,
                "required": field.required,
                "help_text": field.help_text,
                "data_type": effective.columns[name].physical.data_type,
                "lookup": effective.columns[name].input_mode == ColumnProfile.InputMode.LOOKUP,
                "value": form[name].value(),
            }
            for name, field in form.fields.items()
        ],
        "state_token": state_token,
        "errors": form.errors.get_json_data(),
    }


def _payload(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@require_http_methods(["GET", "POST"])
@staff_required
def row_add(request, schema, table):
    definition, effective = _table(request, schema, table)
    if definition is None:
        return JsonResponse({"error": "Table not found."}, status=404)
    if effective is None:
        return JsonResponse({"error": "Table is not released."}, status=403)
    if not effective.capabilities.can_insert:
        return JsonResponse({"error": "INSERT is not allowed."}, status=403)
    form_class = build_row_form(effective, mode="insert")
    if request.method == "GET":
        return JsonResponse(_row_form_data(form_class(), effective))
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    form = form_class(data)
    if not form.is_valid():
        return JsonResponse(_row_form_data(form, effective), status=400)
    values = {}
    for column in definition.columns:
        if not effective.capabilities.columns[column.name].can_insert or column.name not in form.fields:
            continue
        if data.get(column.name, "") == "" and column.default is not None:
            continue
        values[column.name] = form.cleaned_data[column.name]
    try:
        MutationService().insert(schema=schema, table=table, values=values, actor=request.user)
    except Exception as exc:
        form.add_error(None, str(exc))
        return JsonResponse(_row_form_data(form, effective), status=400)
    return JsonResponse({"ok": True})


def _owned_row(schema, table, row_token):
    try:
        record_key = load_row_token(row_token)
    except signing.BadSignature:
        return None, None
    if owned_record(schema, table, record_key) is None:
        return None, None
    row = GenericTableRepository().get_row(schema, table, record_key)
    if row is None or verified_owned_record(schema, table, record_key, row) is None:
        return None, None
    return record_key, row


@require_http_methods(["GET", "POST"])
@staff_required
def row_edit(request, schema, table, row_token):
    definition, effective = _table(request, schema, table)
    if definition is None:
        return JsonResponse({"error": "Table not found."}, status=404)
    if effective is None:
        return JsonResponse({"error": "Table is not released."}, status=403)
    if not effective.capabilities.can_update:
        return JsonResponse({"error": "UPDATE is not allowed."}, status=403)
    record_key, current = _owned_row(schema, table, row_token)
    if current is None:
        return JsonResponse({"error": "Managed row not found."}, status=404)
    form_class = build_row_form(effective, mode="update")
    if request.method == "GET":
        initial = {name: current[name] for name in form_class.base_fields}
        form = form_class(initial=initial)
        return JsonResponse(_row_form_data(form, effective, make_state_token(record_key, current)))
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    form = form_class(data)
    if not form.is_valid():
        return JsonResponse(_row_form_data(form, effective, data.get("state_token", "")), status=400)
    try:
        state_key, fingerprint = load_state_token(data.get("state_token", ""))
    except signing.BadSignature:
        return JsonResponse({"error": "Invalid row state token."}, status=403)
    if state_key != record_key:
        return JsonResponse({"error": "Row state mismatch."}, status=403)
    values = {
        name: form.cleaned_data[name] for name in form.fields
        if effective.capabilities.columns[name].can_update
    }
    try:
        MutationService().update(
            schema=schema, table=table, record_key=record_key, values=values,
            expected_fingerprint=fingerprint, actor=request.user,
        )
    except StaleRowError:
        return JsonResponse({"error": "Row changed after opening the form. Reload it."}, status=409)
    except RowNotFound:
        return JsonResponse({"error": "Row not found."}, status=404)
    except Exception as exc:
        form.add_error(None, str(exc))
        return JsonResponse(_row_form_data(form, effective, data.get("state_token", "")), status=400)
    return JsonResponse({"ok": True})


@require_http_methods(["GET", "POST"])
@staff_required
def row_delete(request, schema, table, row_token):
    definition, effective = _table(request, schema, table)
    if definition is None:
        return JsonResponse({"error": "Table not found."}, status=404)
    if effective is None:
        return JsonResponse({"error": "Table is not released."}, status=403)
    if not effective.capabilities.can_delete:
        return JsonResponse({"error": "DELETE is not allowed."}, status=403)
    record_key, current = _owned_row(schema, table, row_token)
    if current is None:
        return JsonResponse({"error": "Managed row not found."}, status=404)
    if request.method == "GET":
        return JsonResponse({"values": {name: _display_value(value) for name, value in current.items()},
                             "state_token": make_state_token(record_key, current)})
    data = _payload(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        state_key, fingerprint = load_state_token(data.get("state_token", ""))
    except signing.BadSignature:
        return JsonResponse({"error": "Invalid row state token."}, status=403)
    if state_key != record_key:
        return JsonResponse({"error": "Row state mismatch."}, status=403)
    try:
        MutationService().delete(
            schema=schema, table=table, record_key=record_key,
            expected_fingerprint=fingerprint, actor=request.user,
        )
    except StaleRowError:
        return JsonResponse({"error": "Row changed after opening the delete page."}, status=409)
    except RowNotFound:
        return JsonResponse({"error": "Row not found."}, status=404)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})


@require_GET
@staff_required
def lookup_search(request, schema, table, column):
    return legacy_lookup_search(request, schema, table, column)


@require_GET
@staff_required
def configuration(request):
    return JsonResponse({"tables": database_configuration_tables(request)})


@staff_required
def table_review(request, schema, table):
    return legacy_table_review(request, schema, table)


@staff_required
def table_release(request, schema, table):
    return legacy_table_release(request, schema, table)


@staff_required
def table_unrelease(request, schema, table):
    return legacy_table_unrelease(request, schema, table)


def _root(request):
    if not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can view operation logs.")


@require_GET
@staff_required
def operation_list(request):
    _root(request)
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    logs = OperationLog.objects.select_related("actor").order_by("-created_at")
    total = logs.count()
    page_size = 50
    items = logs[(page - 1) * page_size:page * page_size]
    events = ConfigurationEvent.objects.select_related("actor")[:50] if page == 1 else []
    return JsonResponse({
        "page": page,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "operations": [
            {
                "id": str(item.pk),
                "created_at": item.created_at,
                "action": item.action,
                "schema": item.schema_name,
                "table": item.table_name,
                "status": item.status,
                "actor": item.actor.get_username() if item.actor else None,
            }
            for item in items
        ],
        "configuration_events": [
            {
                "created_at": item.created_at,
                "schema": item.schema_name,
                "table": item.table_name,
                "action": item.action,
                "actor": item.actor.get_username() if item.actor else None,
                "was_released": item.was_released,
                "is_released": item.is_released,
            }
            for item in events
        ],
    })


@require_GET
@staff_required
def operation_detail(request, operation_id):
    _root(request)
    try:
        item = OperationLog.objects.select_related("actor").get(pk=operation_id)
    except OperationLog.DoesNotExist:
        return JsonResponse({"error": "Operation not found."}, status=404)
    return JsonResponse({
        "id": str(item.pk),
        "created_at": item.created_at,
        "action": item.action,
        "schema": item.schema_name,
        "table": item.table_name,
        "status": item.status,
        "actor": item.actor.get_username() if item.actor else None,
        "record_key": item.record_key,
        "before_data": item.before_data,
        "after_data": item.after_data,
        "error_message": item.error_message,
        "rolled_back_at": item.rolled_back_at,
        "can_rollback": item.status in {OperationLog.Status.APPLIED, OperationLog.Status.ROLLBACK_FAILED},
    })


@require_POST
@staff_required
def operation_rollback(request, operation_id):
    _root(request)
    try:
        item = OperationLog.objects.get(pk=operation_id)
    except OperationLog.DoesNotExist:
        return JsonResponse({"error": "Operation not found."}, status=404)
    if item.status not in {OperationLog.Status.APPLIED, OperationLog.Status.ROLLBACK_FAILED}:
        return JsonResponse({"error": "This operation cannot be rolled back."}, status=403)
    try:
        RollbackService().rollback(item)
    except Exception as exc:
        return JsonResponse({"error": f"Rollback blocked: {exc}"}, status=409)
    return JsonResponse({"ok": True})
