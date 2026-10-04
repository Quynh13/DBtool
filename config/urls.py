from django.contrib import admin
from django.urls import include, path

from dbtool import api


urlpatterns = [
    path("app/", api.frontend, name="frontend"),
    path("api/v1/session/", api.session, name="api_session"),
    path("api/v1/tables/", api.table_list, name="api_table_list"),
    path("api/v1/tables/<str:schema>/<str:table>/", api.table_detail, name="api_table_detail"),
    path("api/v1/tables/<str:schema>/<str:table>/add/", api.row_add, name="api_row_add"),
    path("api/v1/tables/<str:schema>/<str:table>/rows/<str:row_token>/edit/", api.row_edit, name="api_row_edit"),
    path("api/v1/tables/<str:schema>/<str:table>/rows/<str:row_token>/delete/", api.row_delete, name="api_row_delete"),
    path("api/v1/tables/<str:schema>/<str:table>/lookup/<str:column>/", api.lookup_search, name="api_lookup_search"),
    path("api/v1/configuration/", api.configuration, name="api_configuration"),
    path("api/v1/configuration/<str:schema>/<str:table>/", api.table_review, name="api_table_review"),
    path("api/v1/configuration/<str:schema>/<str:table>/release/", api.table_release, name="api_table_release"),
    path("api/v1/configuration/<str:schema>/<str:table>/unrelease/", api.table_unrelease, name="api_table_unrelease"),
    path("api/v1/operations/", api.operation_list, name="api_operation_list"),
    path("api/v1/operations/<uuid:operation_id>/", api.operation_detail, name="api_operation_detail"),
    path("api/v1/operations/<uuid:operation_id>/rollback/", api.operation_rollback, name="api_operation_rollback"),
    path(
        "admin/db-editor/",
        include("dbtool.urls"),
    ),
    path(
        "admin/",
        admin.site.urls,
    ),
]
