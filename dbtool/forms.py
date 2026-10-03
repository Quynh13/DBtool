from django import forms

from dbtool.models import ColumnProfile
from dbtool.repository import GenericTableRepository


class TableReviewForm(forms.Form):
    display_label = forms.CharField(required=False, max_length=200)
    mutation_enabled = forms.BooleanField(
        required=False,
        initial=True,
        label="Enable mutations when configuration is complete",
    )


class ColumnReviewForm(forms.Form):
    reviewed = forms.BooleanField(
        required=False,
        label="Reviewed",
    )
    display_label = forms.CharField(required=False, max_length=200)
    show_in_form = forms.BooleanField(required=False)
    editable = forms.BooleanField(required=False)
    required_on_insert = forms.BooleanField(required=False)
    allow_clear_on_update = forms.BooleanField(required=False)
    input_mode = forms.ChoiceField(choices=ColumnProfile.InputMode.choices)
    help_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    target_schema = forms.ChoiceField(required=False)
    target_table = forms.ChoiceField(required=False)
    target_value_column = forms.ChoiceField(required=False)
    display_columns = forms.MultipleChoiceField(required=False)
    search_columns = forms.MultipleChoiceField(required=False)
    allow_null = forms.BooleanField(required=False)
    allow_arbitrary_value = forms.BooleanField(required=False)
    output_mappings = forms.JSONField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, catalog, destination_columns=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog = catalog
        self.destination_columns = set(destination_columns)
        schemas = sorted(catalog)
        selected_schema = self._selected("target_schema")
        if selected_schema not in catalog:
            selected_schema = schemas[0] if schemas else ""
        tables = sorted(catalog.get(selected_schema, {}))
        selected_table = self._selected("target_table")
        if selected_table not in tables:
            selected_table = tables[0] if tables else ""
        columns = catalog.get(selected_schema, {}).get(selected_table, [])

        self.fields["target_schema"].choices = [
            (schema, schema) for schema in schemas
        ]
        self.fields["target_table"].choices = [
            (table, table) for table in tables
        ]
        column_choices = [(column, column) for column in columns]
        self.fields["target_value_column"].choices = column_choices
        self.fields["display_columns"].choices = column_choices
        self.fields["search_columns"].choices = column_choices

    def _selected(self, name):
        if self.is_bound:
            return self.data.get(self.add_prefix(name), "")
        return self.initial.get(name, "")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("input_mode") != ColumnProfile.InputMode.LOOKUP:
            cleaned["output_mappings"] = []
            return cleaned

        for name in (
            "target_schema",
            "target_table",
            "target_value_column",
            "display_columns",
            "search_columns",
        ):
            if not cleaned.get(name):
                self.add_error(name, "Required for lookup input mode.")
        mappings = cleaned.get("output_mappings") or []
        cleaned["output_mappings"] = mappings
        source_names = set(self.catalog.get(cleaned.get("target_schema"), {}).get(cleaned.get("target_table"), []))
        if not isinstance(mappings, list) or any(
            not isinstance(pair, dict)
            or set(pair) != {"source", "destination"}
            or not isinstance(pair["source"], str)
            or not isinstance(pair["destination"], str)
            or pair["source"] not in source_names
            or pair["destination"] not in self.destination_columns
            for pair in mappings
        ):
            self.add_error("output_mappings", "Choose valid source and destination columns.")
        return cleaned


def _boolean_field(required: bool, **kwargs):
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
        **kwargs,
    )


def _field_for_column(column, required: bool, *, label=None, help_text=""):
    common = {
        "label": label or column.name,
        "required": required,
        "help_text": help_text,
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
        return _boolean_field(
            required,
            label=common["label"],
            help_text=common["help_text"],
        )

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

    return forms.CharField(empty_value=None, **common)


def build_row_form(
    effective,
    *,
    mode: str,
    lookup_urls=None,
):
    fields = {}
    lookup_urls = lookup_urls or {}

    for column in effective.definition.columns:
        effective_column = effective.columns[column.name]
        capability = effective.capabilities.columns[column.name]

        if not effective_column.show_in_form:
            continue

        if mode == "insert":
            if not capability.can_insert:
                continue

            required = capability.required_on_insert

        elif mode == "update":
            if not capability.can_update:
                continue

            # Semantic-invalid legacy values may remain unchanged. The mutation
            # validator decides whether an update is actually clearing a value.
            required = not column.nullable

        else:
            raise ValueError(f"Unsupported form mode: {mode}")

        field = _field_for_column(
            column,
            required,
            label=effective_column.label,
            help_text=effective_column.help_text,
        )
        if effective_column.input_mode == ColumnProfile.InputMode.LOOKUP:
            field.widget = forms.Select(
                choices=[("", f"Search {effective_column.label}...")],
                attrs={
                    "data-lookup-url": lookup_urls.get(column.name, ""),
                    "data-lookup-label": effective_column.label,
                },
            )
        fields[column.name] = field

    return type(
        "GenericRowForm",
        (forms.Form,),
        fields,
    )


def populate_lookup_choices(form, effective, initial_values=None):
    initial_values = initial_values or {}
    repository = GenericTableRepository()

    for name, effective_column in effective.columns.items():
        if name not in form.fields or effective_column.lookup is None:
            continue
        lookup = effective_column.lookup
        choices = [("", f"Search {effective_column.label}...")]
        choices.extend(
            (item["value"], item["label"])
            for item in repository.search_lookup_rows(
                lookup.target_schema,
                lookup.target_table,
                value_column=lookup.target_value_column,
                display_columns=lookup.display_columns,
                search_columns=lookup.search_columns,
            )
        )
        current = initial_values.get(name)
        if current not in (None, "") and str(current) not in {
            value for value, _label in choices
        }:
            choices.append((str(current), str(current)))
        form.fields[name].widget.choices = choices
