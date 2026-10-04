from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from dbtool.views import released_tables


@require_GET
def frontend(request):
    return render(request, "index.html")


@require_GET
def table_list(request):
    return JsonResponse({"tables": released_tables()})
