from functools import wraps

from django.core.exceptions import PermissionDenied


def can_use_tool(user):
    return bool(
        user.is_authenticated and user.is_active and user.is_staff
        and (user.is_superuser or user.groups.filter(name="dbtool").exists())
    )


def can_access_record(user, owned):
    return bool(
        user and owned and (user.is_superuser or owned.origin.actor_id == user.pk)
    )


def tool_access_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not can_use_tool(request.user):
            raise PermissionDenied("The dbtool group is required.")
        return view(request, *args, **kwargs)

    return wrapped
