from django.contrib import admin
from django.urls import include, path


urlpatterns = [
    path(
        "admin/dbtool/",
        include("dbtool.urls"),
    ),
    path(
        "admin/",
        admin.site.urls,
    ),
]
