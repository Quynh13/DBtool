from django.contrib import admin
from django.urls import include, path

from dbtool import api


urlpatterns = [
    path("app/", api.frontend, name="frontend"),
    path("api/v1/session/", api.session, name="api_session"),
    path("api/v1/tables/", api.table_list, name="api_table_list"),
    path("api/v1/tables/<str:schema>/<str:table>/", api.table_detail, name="api_table_detail"),
    path(
        "admin/db-editor/",
        include("dbtool.urls"),
    ),
    path(
        "admin/",
        admin.site.urls,
    ),
]
