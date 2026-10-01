import hashlib
import json

from django.core import signing

from audit.codec import decode_row, encode_row


ROW_KEY_SALT = "dbtool.row-key.v1"
ROW_STATE_SALT = "dbtool.row-state.v1"


def row_fingerprint(row: dict) -> str:
    encoded = encode_row(row)

    payload = json.dumps(
        encoded,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_row_token(record_key: dict) -> str:
    return signing.dumps(
        encode_row(record_key),
        salt=ROW_KEY_SALT,
        compress=True,
    )


def load_row_token(token: str) -> dict:
    encoded = signing.loads(
        token,
        salt=ROW_KEY_SALT,
    )

    return decode_row(encoded)


def make_state_token(
    record_key: dict,
    row: dict,
) -> str:
    payload = {
        "record_key": encode_row(record_key),
        "fingerprint": row_fingerprint(row),
    }

    return signing.dumps(
        payload,
        salt=ROW_STATE_SALT,
        compress=True,
    )


def load_state_token(token: str):
    payload = signing.loads(
        token,
        salt=ROW_STATE_SALT,
    )

    return (
        decode_row(payload["record_key"]),
        payload["fingerprint"],
    )
