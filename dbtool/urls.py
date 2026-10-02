from django.contrib import admin
from django.urls import path

from dbtool import views


app_name = "dbtool"


urlpatterns = [
    path(
        "",
        admin.site.admin_view(views.table_list),
        name="table_list",
    ),
    path(
        "<str:schema>/<str:table>/add/",
        admin.site.admin_view(views.row_add),
        name="row_add",
    ),
    path(
        "<str:schema>/<str:table>/review/",
        admin.site.admin_view(views.table_review),
        name="table_review",
    ),
    path(
        "<str:schema>/<str:table>/lookup/<str:column>/",
        admin.site.admin_view(views.lookup_search),
        name="lookup_search",
    ),
    path(
        "<str:schema>/<str:table>/row/<str:row_token>/edit/",
        admin.site.admin_view(views.row_edit),
        name="row_edit",
    ),
    path(
        "<str:schema>/<str:table>/row/<str:row_token>/delete/",
        admin.site.admin_view(views.row_delete),
        name="row_delete",
    ),
    path(
        "operation/<uuid:operation_id>/rollback/",
        admin.site.admin_view(views.operation_rollback),
        name="operation_rollback",
    ),
    path(
        "<str:schema>/<str:table>/",
        admin.site.admin_view(views.table_detail),
        name="table_detail",
    ),
]
