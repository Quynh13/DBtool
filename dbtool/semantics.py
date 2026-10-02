from dataclasses import dataclass, field, replace

from dbtool.introspection import ColumnDefinition, TableDefinition
from dbtool.models import ColumnProfile, LookupMapping, TableProfile
from dbtool.safety import ColumnCapability, SafetyPolicy, TableCapabilities


READY = "READY"
NEEDS_REVIEW = "NEEDS REVIEW"
READ_ONLY = "READ ONLY"


@dataclass(frozen=True)
class EffectiveColumn:
    physical: ColumnDefinition
    capability: ColumnCapability
    profile: ColumnProfile | None
    lookup: LookupMapping | None
    label: str
    show_in_form: bool
    editable: bool
    required_on_insert: bool
    allow_clear_on_update: bool
    input_mode: str | None
    help_text: str
    issues: list[str] = field(default_factory=list)

    @property
    def system_managed(self):
        return not self.show_in_form or not self.editable


@dataclass(frozen=True)
class EffectiveTable:
    definition: TableDefinition
    profile: TableProfile | None
    status: str
    capabilities: TableCapabilities
    columns: dict[str, EffectiveColumn]
    warnings: list[str]

    @property
    def display_label(self):
        if self.profile and self.profile.display_label:
            return self.profile.display_label
        return self.definition.name


class EffectiveMetadataService:
    def __init__(self, *, introspector=None):
        from dbtool.introspection import SchemaIntrospector

        self.introspector = introspector or SchemaIntrospector()
        self.policy = SafetyPolicy()

    def resolve(self, definition: TableDefinition) -> EffectiveTable:
        physical = self.policy.evaluate(definition)
        profile = (
            TableProfile.objects.filter(
                schema_name=definition.schema,
                table_name=definition.name,
            )
            .prefetch_related("column_profiles__lookup_mapping")
            .first()
        )
        profiles = (
            {item.column_name: item for item in profile.column_profiles.all()}
            if profile
            else {}
        )
        warnings = list(physical.warnings)
        columns = {}
        configured_names = set(profiles)
        physical_names = {column.name for column in definition.columns}

        for column in definition.columns:
            capability = physical.columns[column.name]
            column_profile = profiles.get(column.name)
            lookup = None
            issues = []

            if column_profile is None:
                issues.append("Semantic metadata is not configured.")
                label = column.name
                show_in_form = False
                editable = False
                required = capability.required_on_insert
                allow_clear = column.nullable
                input_mode = None
                help_text = ""
            else:
                label = column_profile.display_label or column.name
                show_in_form = column_profile.show_in_form
                editable = column_profile.editable
                required = (
                    capability.required_on_insert
                    or column_profile.required_on_insert
                )
                allow_clear = (
                    column.nullable and column_profile.allow_clear_on_update
                )
                input_mode = column_profile.input_mode
                help_text = column_profile.help_text

                if not column_profile.reviewed:
                    issues.append("Semantic metadata has not been reviewed.")

                if editable and not show_in_form:
                    issues.append("Editable columns must be shown on the form.")

                if editable and not (
                    capability.can_insert or capability.can_update
                ):
                    issues.append(
                        "Physical metadata does not allow this column to be edited."
                    )

                try:
                    lookup = column_profile.lookup_mapping
                except LookupMapping.DoesNotExist:
                    lookup = None

                if input_mode == ColumnProfile.InputMode.LOOKUP:
                    if lookup is None:
                        issues.append("Lookup mapping is not configured.")
                    else:
                        issues.extend(self._validate_lookup(lookup))
                        required = required or not lookup.allow_null
                        allow_clear = allow_clear and lookup.allow_null
                        source_help = (
                            f"Select from {lookup.target_schema}."
                            f"{lookup.target_table}; saves "
                            f"{lookup.target_value_column}."
                        )
                        help_text = " ".join(
                            part for part in (help_text, source_help) if part
                        )
                elif lookup is not None:
                    issues.append("Lookup mapping requires lookup input mode.")

                if required and not (
                    capability.can_insert and editable and show_in_form
                ):
                    issues.append(
                        "Required insert value cannot be entered on the form."
                    )

            effective_capability = replace(
                capability,
                can_insert=(
                    capability.can_insert and editable and show_in_form
                ),
                can_update=(
                    capability.can_update and editable and show_in_form
                ),
                required_on_insert=required,
                warnings=capability.warnings + issues,
            )
            columns[column.name] = EffectiveColumn(
                physical=column,
                capability=effective_capability,
                profile=column_profile,
                lookup=lookup,
                label=label,
                show_in_form=show_in_form,
                editable=editable,
                required_on_insert=required,
                allow_clear_on_update=allow_clear,
                input_mode=input_mode,
                help_text=help_text,
                issues=issues,
            )
            warnings.extend(f"{column.name}: {issue}" for issue in issues)

        stale_names = configured_names - physical_names
        if stale_names:
            warnings.append(
                "Configured columns no longer present in the target table: "
                + ", ".join(sorted(stale_names))
            )

        physically_mutable = any(
            (
                physical.can_insert,
                physical.can_update,
                physical.can_delete,
            )
        )
        incomplete = any(column.issues for column in columns.values()) or bool(
            stale_names
        )

        if not physically_mutable or (profile and not profile.mutation_enabled):
            status = READ_ONLY
        elif profile is None or incomplete:
            status = NEEDS_REVIEW
        else:
            status = READY

        mutations_allowed = status == READY
        effective_capabilities = TableCapabilities(
            can_read=True,
            can_insert=physical.can_insert and mutations_allowed,
            can_update=(
                physical.can_update
                and mutations_allowed
                and any(item.capability.can_update for item in columns.values())
            ),
            can_delete=physical.can_delete and mutations_allowed,
            warnings=warnings,
            columns={
                name: (
                    item.capability
                    if mutations_allowed
                    else replace(
                        item.capability,
                        can_insert=False,
                        can_update=False,
                    )
                )
                for name, item in columns.items()
            },
        )
        return EffectiveTable(
            definition=definition,
            profile=profile,
            status=status,
            capabilities=effective_capabilities,
            columns=columns,
            warnings=warnings,
        )

    def _validate_lookup(self, lookup):
        issues = []
        display_columns = lookup.display_columns
        search_columns = lookup.search_columns

        if not self._valid_column_list(display_columns):
            issues.append("Lookup display columns must be a non-empty list.")
        if not self._valid_column_list(search_columns):
            issues.append("Lookup search columns must be a non-empty list.")
        if issues:
            return issues

        try:
            target = self.introspector.inspect_table(
                lookup.target_schema,
                lookup.target_table,
            )
        except ValueError:
            return ["Lookup target table does not exist."]

        target_names = {column.name for column in target.columns}
        configured = {
            lookup.target_value_column,
            *display_columns,
            *search_columns,
        }
        missing = configured - target_names
        if missing:
            issues.append(
                "Lookup target columns do not exist: "
                + ", ".join(sorted(missing))
            )
        return issues

    @staticmethod
    def _valid_column_list(value):
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) and item for item in value)
        )
