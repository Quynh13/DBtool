import json
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods

from dbtool.views import released_tables, table_detail_context


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
        return view(request, *args, **kwargs)
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
