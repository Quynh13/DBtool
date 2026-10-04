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
    path(
        "admin/db-editor/",
        include("dbtool.urls"),
    ),
    path(
        "admin/",
        admin.site.urls,
    ),
]
