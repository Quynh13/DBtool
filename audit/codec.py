import base64
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID


TYPE_MARKER = "__dbtool_type__"


def encode_value(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return {
            TYPE_MARKER: "float",
            "value": repr(value),
        }

    if isinstance(value, str):
        return value

    if isinstance(value, Decimal):
        return {
            TYPE_MARKER: "decimal",
            "value": str(value),
        }

    if isinstance(value, UUID):
        return {
            TYPE_MARKER: "uuid",
            "value": str(value),
        }

    if isinstance(value, datetime):
        return {
            TYPE_MARKER: "datetime",
            "value": value.isoformat(),
        }

    if isinstance(value, date):
        return {
            TYPE_MARKER: "date",
            "value": value.isoformat(),
        }

    if isinstance(value, time):
        return {
            TYPE_MARKER: "time",
            "value": value.isoformat(),
        }

    if isinstance(value, memoryview):
        value = value.tobytes()

    if isinstance(value, bytes):
        return {
            TYPE_MARKER: "bytes",
            "value": base64.b64encode(value).decode("ascii"),
        }

    if isinstance(value, list):
        return [encode_value(item) for item in value]

    if isinstance(value, tuple):
        return {
            TYPE_MARKER: "tuple",
            "value": [encode_value(item) for item in value],
        }

    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}

    raise TypeError(f"Unsupported snapshot value type: {type(value).__name__}")


def decode_value(value):
    if isinstance(value, list):
        return [decode_value(item) for item in value]

    if not isinstance(value, dict):
        return value

    value_type = value.get(TYPE_MARKER)

    if value_type == "float":
        return float(value["value"])

    if value_type == "decimal":
        return Decimal(value["value"])

    if value_type == "uuid":
        return UUID(value["value"])

    if value_type == "datetime":
        return datetime.fromisoformat(value["value"])

    if value_type == "date":
        return date.fromisoformat(value["value"])

    if value_type == "time":
        return time.fromisoformat(value["value"])

    if value_type == "bytes":
        return base64.b64decode(value["value"])

    if value_type == "tuple":
        return tuple(decode_value(item) for item in value["value"])

    return {key: decode_value(item) for key, item in value.items()}


def encode_row(row: dict | None):
    if row is None:
        return None

    return {key: encode_value(value) for key, value in row.items()}


def decode_row(row: dict | None):
    if row is None:
        return None

    return {key: decode_value(value) for key, value in row.items()}
