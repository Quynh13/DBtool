from datetime import date, datetime, time
from decimal import Decimal
from uuid import uuid4

from django.test import SimpleTestCase

from audit.codec import decode_row, encode_row


class SnapshotCodecTests(SimpleTestCase):
    def test_round_trip_supported_values(self):
        original = {
            "integer": 123,
            "float": 10.25,
            "boolean": True,
            "text": "hello",
            "none": None,
            "decimal": Decimal("123.456"),
            "uuid": uuid4(),
            "datetime": datetime(2026, 10, 2, 12, 30, 15),
            "date": date(2026, 10, 2),
            "time": time(12, 30, 15),
            "bytes": b"\x00\x01\x02",
            "memoryview": memoryview(b"\x03\x04"),
            "list": [
                1,
                Decimal("2.5"),
            ],
            "dict": {
                "enabled": True,
            },
        }

        encoded = encode_row(original)
        decoded = decode_row(encoded)

        self.assertEqual(
            decoded["integer"],
            original["integer"],
        )

        self.assertEqual(
            decoded["float"],
            original["float"],
        )

        self.assertEqual(
            decoded["decimal"],
            original["decimal"],
        )

        self.assertEqual(
            decoded["uuid"],
            original["uuid"],
        )

        self.assertEqual(
            decoded["datetime"],
            original["datetime"],
        )

        self.assertEqual(
            decoded["date"],
            original["date"],
        )

        self.assertEqual(
            decoded["time"],
            original["time"],
        )

        self.assertEqual(
            decoded["bytes"],
            original["bytes"],
        )

        self.assertEqual(
            decoded["memoryview"],
            b"\x03\x04",
        )

        self.assertEqual(
            decoded["list"],
            original["list"],
        )

        self.assertEqual(
            decoded["dict"],
            original["dict"],
        )
