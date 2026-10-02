from dbtool.semantics import READY


class SemanticValidationError(ValueError):
    pass


class SemanticValidator:
    def __init__(self, repository):
        self.repository = repository

    def validate_insert(self, effective, values):
        self._require_ready(effective)

        for name, column in effective.columns.items():
            value = values.get(name)
            if column.required_on_insert and self._empty(value):
                raise SemanticValidationError(f"{column.label} is required.")
            if name in values:
                self._validate_lookup(column, value)

    def validate_update(self, effective, before, values):
        self._require_ready(effective)

        for name, value in values.items():
            column = effective.columns[name]
            previous = before.get(name)

            if value == previous:
                continue
            if self._empty(value) and not self._empty(previous):
                if not column.allow_clear_on_update:
                    raise SemanticValidationError(
                        f"{column.label} cannot be cleared."
                    )
            self._validate_lookup(column, value)

    @staticmethod
    def _require_ready(effective):
        if effective.status != READY:
            raise SemanticValidationError(
                "Semantic metadata is not ready; mutations are disabled."
            )

    def _validate_lookup(self, column, value):
        lookup = column.lookup
        if lookup is None or lookup.allow_arbitrary_value:
            return
        if self._empty(value):
            if not lookup.allow_null:
                raise SemanticValidationError(f"{column.label} is required.")
            return
        if not self.repository.lookup_value_exists(
            lookup.target_schema,
            lookup.target_table,
            lookup.target_value_column,
            value,
        ):
            raise SemanticValidationError(
                f"Select a valid value for {column.label}."
            )

    @staticmethod
    def _empty(value):
        return value is None or value == ""
