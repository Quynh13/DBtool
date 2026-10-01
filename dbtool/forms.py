from django import forms

from dbtool.introspection import TableDefinition
from dbtool.safety import TableCapabilities


def _boolean_field(required: bool):
    choices = []

    if not required:
        choices.append(("", "---------"))

    choices.extend(
        [
            ("true", "True"),
            ("false", "False"),
        ]
    )

    def coerce(value):
        if value in ("true", True):
            return True

        if value in ("false", False):
            return False

        return None

    return forms.TypedChoiceField(
        choices=choices,
        coerce=coerce,
        empty_value=None,
        required=required,
    )


def _field_for_column(column, required: bool):
    common = {
        "label": column.name,
        "required": required,
    }

    data_type = column.data_type

    if data_type in {
        "smallint",
        "integer",
        "bigint",
    }:
        return forms.IntegerField(**common)

    if data_type in {
        "numeric",
        "decimal",
    }:
        return forms.DecimalField(**common)

    if data_type in {
        "real",
        "double precision",
    }:
        return forms.FloatField(**common)

    if data_type == "boolean":
        return _boolean_field(required)

    if data_type == "date":
        return forms.DateField(
            widget=forms.DateInput(
                attrs={"type": "date"},
            ),
            **common,
        )

    if data_type in {
        "timestamp without time zone",
        "timestamp with time zone",
    }:
        return forms.DateTimeField(
            widget=forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            input_formats=[
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%dT%H:%M:%S",
            ],
            **common,
        )

    if data_type in {
        "time without time zone",
        "time with time zone",
    }:
        return forms.TimeField(
            widget=forms.TimeInput(
                attrs={"type": "time"},
            ),
            **common,
        )

    if data_type == "uuid":
        return forms.UUIDField(**common)

    if data_type in {
        "json",
        "jsonb",
    }:
        return forms.JSONField(
            widget=forms.Textarea(
                attrs={"rows": 6},
            ),
            **common,
        )

    return forms.CharField(**common)


def build_row_form(
    definition: TableDefinition,
    capabilities: TableCapabilities,
    *,
    mode: str,
):
    fields = {}

    for column in definition.columns:
        capability = capabilities.columns[column.name]

        if mode == "insert":
            if not capability.can_insert:
                continue

            required = capability.required_on_insert

        elif mode == "update":
            if not capability.can_update:
                continue

            required = not column.nullable

        else:
            raise ValueError(f"Unsupported form mode: {mode}")

        fields[column.name] = _field_for_column(
            column,
            required,
        )

    return type(
        "GenericRowForm",
        (forms.Form,),
        fields,
    )
