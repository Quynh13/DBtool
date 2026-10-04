from django.contrib import admin
from django.urls import include, path

from dbtool import api


urlpatterns = [
    path("app/", admin.site.admin_view(api.frontend), name="frontend"),
    path("api/v1/tables/", admin.site.admin_view(api.table_list), name="api_table_list"),
    path(
        "admin/db-editor/",
        include("dbtool.urls"),
    ),
    path(
        "admin/",
        admin.site.urls,
    ),
]
