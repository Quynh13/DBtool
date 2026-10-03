import hashlib
import json

from audit.codec import decode_row, encode_row
from audit.models import ToolOwnedRecord
from dbtool.row_state import row_fingerprint


def key_digest(record_key):
    payload = json.dumps(encode_row(record_key), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def owned_record(schema, table, record_key):
    record = ToolOwnedRecord.objects.filter(
        schema_name=schema, table_name=table, key_digest=key_digest(record_key), active=True
    ).first()
    if record and decode_row(record.record_key) == record_key:
        return record
    return None


def verified_owned_record(schema, table, record_key, row):
    record = owned_record(schema, table, record_key)
    if record is None or row_fingerprint(row) != record.fingerprint:
        return None
    return record
