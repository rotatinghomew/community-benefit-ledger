"""核对交换资料的客户编号、时序和关联条件。"""
import json
from pathlib import Path
import unittest


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def rows(name):
    content = (SAMPLES / f"{name}.ndjson").read_text(encoding="utf-8")
    return [json.loads(line) for line in content.splitlines()]


class ExchangeSampleTest(unittest.TestCase):
    def test_records_are_json_objects(self):
        files = sorted(SAMPLES.glob("*.ndjson"))
        self.assertEqual(len(files), 6)
        for path in files:
            with self.subTest(path=path.name):
                records = rows(path.stem)
                self.assertTrue(records)
                self.assertTrue(all(isinstance(row, dict) for row in records))

    def test_related_records_preserve_source_evidence(self):
        lease = rows("terminals")[0]
        grants = {row["grant"]: row for row in rows("grants")}
        self.assertLess(sum(lease["allocations"].values()), sum(row["amount"] for row in grants.values()))
        self.assertTrue(all(amount <= grants[code]["amount"] for code, amount in lease["allocations"].items()))
        final_seq = rows("closures")[0]["last_seq"]
        receipts = rows("receipts")
        self.assertEqual([row["seq"] for row in receipts], [1, final_seq, 2, 1])
        revocation = rows("mandates")[0]["revoked_at"]
        self.assertLess(receipts[0]["occurred_at"], revocation)
        self.assertGreater(receipts[1]["occurred_at"], revocation)
