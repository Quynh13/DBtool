from django.contrib import admin
from django.urls import path

from dbtool import views
from dbtool.access import tool_access_required


app_name = "dbtool"


def editor_view(view):
    return admin.site.admin_view(tool_access_required(view))


urlpatterns = [
    path(
        "",
        editor_view(views.table_list),
        name="table_list",
    ),
    path(
        "configuration/",
        editor_view(views.database_configuration),
        name="database_configuration",
    ),
    path(
        "configuration/<str:schema>/<str:table>/release/",
        editor_view(views.table_release),
        name="table_release",
    ),
    path(
        "configuration/<str:schema>/<str:table>/unrelease/",
        editor_view(views.table_unrelease),
        name="table_unrelease",
    ),
    path(
        "<str:schema>/<str:table>/add/",
        editor_view(views.row_add),
        name="row_add",
    ),
    path(
        "<str:schema>/<str:table>/review/",
        editor_view(views.table_review),
        name="table_review",
    ),
    path(
        "<str:schema>/<str:table>/lookup/<str:column>/",
        editor_view(views.lookup_search),
        name="lookup_search",
    ),
    path(
        "<str:schema>/<str:table>/row/<str:row_token>/edit/",
        editor_view(views.row_edit),
        name="row_edit",
    ),
    path(
        "<str:schema>/<str:table>/row/<str:row_token>/delete/",
        editor_view(views.row_delete),
        name="row_delete",
    ),
    path(
        "operation/<uuid:operation_id>/rollback/",
        editor_view(views.operation_rollback),
        name="operation_rollback",
    ),
    path(
        "<str:schema>/<str:table>/",
        editor_view(views.table_detail),
        name="table_detail",
    ),
]
