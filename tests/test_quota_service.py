"""Behavioral tests for the quota and merchant voucher service."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quota_service import (  # noqa: E402
    AuthorizationError,
    ConfirmationRequiredError,
    QuotaService,
)
from quota_service.http_app import create_server  # noqa: E402

SAMPLES = ROOT / "samples"
NOV_29_AM = "2026-11-29T10:00:00+08:00"
NOV_29_PM = "2026-11-29T15:00:00+08:00"
DEC_01 = "2026-12-01T10:00:00+08:00"


class SampleWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.service = QuotaService()
        self.counts = self.service.load_ndjson_dir(SAMPLES)

    def test_samples_loaded(self):
        self.assertEqual(self.counts["grants"], 4)
        self.assertEqual(self.counts["receipts"], 4)
        self.assertEqual(len(self.service.receipts), 3)  # (lease, seq) identities

    def test_quote_respects_specific_shared_self_waterfall_and_lease_hold(self):
        quote = self.service.quote_cart("c-a", NOV_29_AM)
        self.assertEqual(quote["total_amount"], 5500)
        self.assertEqual(quote["grant_amount"], 5100)
        self.assertEqual(quote["self_pay_amount"], 400)

        a1 = next(line for line in quote["lines"] if line["line"] == "a1")
        amounts = [(p["type"], p.get("grant"), p["amount"]) for p in a1["funding"]]
        self.assertEqual(
            amounts,
            [("grant", "g-food", 500), ("grant", "g-shared", 2600)],
        )
        self.assertEqual(a1["funding"][0]["expires_at"], "2026-12-01T00:00:00+08:00")

        a2 = next(line for line in quote["lines"] if line["line"] == "a2")
        self.assertEqual(
            [(p["type"], p.get("grant"), p["amount"]) for p in a2["funding"]],
            [("grant", "g-shared", 2000), ("self_pay", None, 400)],
        )

    def test_self_pay_must_be_explicitly_confirmed(self):
        with self.assertRaises(ConfirmationRequiredError):
            self.service.confirm_cart("c-a", at=NOV_29_AM, actor="help-a")
        order = self.service.confirm_cart(
            "c-a", at=NOV_29_AM, actor="help-a", accept_self_pay=True
        )
        self.assertEqual(order["status"], "awaiting_delivery")

    def test_revised_basket_self_pay_increase_requires_resident(self):
        first = self.service.quote_cart("c-a", NOV_29_AM)
        revised = self.service.revise_cart(
            "c-a",
            [
                {"line": "a1", "category": "fresh", "amount": 3100},
                {"line": "a2", "category": "care", "amount": 3000},
            ],
            NOV_29_AM,
        )
        self.assertGreater(revised["self_pay_amount"], first["self_pay_amount"])
        self.assertTrue(revised["requires_resident_acceptance"])
        rejected = self.service.reject_quote(revised["id"], "resident_will_not_pay_more")
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(ConfirmationRequiredError):
            self.service.confirm_cart(
                "c-a", quote_id=revised["id"], at=NOV_29_AM, accept_self_pay=True
            )

        revised_two = self.service.revise_cart(
            "c-a",
            [
                {"line": "a1", "category": "fresh", "amount": 3100},
                {"line": "a2", "category": "care", "amount": 3000},
            ],
            NOV_29_AM,
        )
        with self.assertRaises(ConfirmationRequiredError):
            self.service.confirm_cart(
                "c-a", quote_id=revised_two["id"], at=NOV_29_AM, accept_self_pay=True
            )
        order = self.service.confirm_cart(
            "c-a",
            quote_id=revised_two["id"],
            at=NOV_29_AM,
            accept_self_pay=True,
            resident_accepts_increase=True,
        )
        self.assertEqual(order["status"], "awaiting_delivery")

    def test_other_resident_specific_grant_is_never_used(self):
        self.service.create_cart(
            {
                "cart": "ca-x",
                "household": "hh8",
                "beneficiary": "res-a",
                "customer": "res-a",
                "merchant": "my",
                "items": [{"line": "x", "category": "fresh", "amount": 10000}],
            }
        )
        quote = self.service.quote_cart("ca-x", NOV_29_AM)
        grants = {p.get("grant") for line in quote["lines"] for p in line["funding"] if p["type"] == "grant"}
        self.assertNotIn("g-other", grants)
        self.assertIn("g-food", grants)
        self.assertIn("g-shared", grants)
        self.assertGreater(quote["self_pay_amount"], 0)

    def test_revoked_delegate_is_blocked_but_operator_can_audit_signed_delivery(self):
        self.service.confirm_cart("c-a", at=NOV_29_AM, actor="help-a", accept_self_pay=True)

        # Delegate confirmation after revocation stops.
        with self.assertRaises(AuthorizationError):
            self.service.confirm_delivery(
                "c-a",
                at=NOV_29_PM,
                actor="help-a",
                signed_at="2026-11-29T13:30:00+08:00",
            )

        # The operator can still audit a purchase already signed before revoke.
        delivery = self.service.confirm_delivery(
            "c-a",
            [{"line": "a1", "confirmed_amount": 3100}],
            at=NOV_29_PM,
            operator="op-li",
            signed_at="2026-11-29T13:30:00+08:00",
        )
        self.assertEqual(delivery["review_status"], "verified")
        self.assertEqual(delivery["actual_amount"], 3100)

    def test_delivery_cancels_hold_and_refunds_original_self_share(self):
        self.service.confirm_cart("c-a", at=NOV_29_AM, actor="help-a", accept_self_pay=True)
        delivery = self.service.confirm_delivery(
            "c-a",
            [{"line": "a1", "confirmed_amount": 3100}],
            at="2026-11-29T13:35:00+08:00",
            signed_at="2026-11-29T13:30:00+08:00",
            actor="help-a",
        )
        self.assertEqual(delivery["actual_amount"], 3100)
        self.assertEqual(delivery["self_refunded_amount"], 400)
        a2 = next(line for line in delivery["lines"] if line["line"] == "a2")
        self.assertEqual(a2["confirmed_amount"], 0)
        self.assertEqual(a2["funding"][-1]["refunds"][0]["amount"], 400)

    def test_offline_duplicate_dispute_closure_and_settlement(self):
        by_seq = {seq: self.service.receipts[("lease1", seq)] for seq in (1, 2, 3)}
        self.assertEqual(by_seq[1]["transport_messages"], ["t11", "t14"])
        self.assertEqual(by_seq[1]["eligibility"], "eligible")
        self.assertEqual(by_seq[2]["eligibility"], "eligible")
        self.assertEqual(by_seq[3]["eligibility"], "disputed_authorization")

        closure = self.service.closures["lease1"][-1]
        self.assertEqual(closure["last_seq"], 3)
        self.assertEqual(closure["status"], "complete")
        self.assertEqual(closure["missing_seqs"], [])

        self.service.verify_lease_receipts("lease1", "op-li", "2026-11-30T18:20:00+08:00")
        settlement = self.service.issue_settlement(
            merchant="mx", items=["lease1#1", "lease1#2"], operator="op-li", at=DEC_01
        )
        self.assertEqual(settlement["voucher_amount"], 2800)
        self.assertEqual(settlement["total_amount"], 2800)

        view = self.service.merchant_view("mx", DEC_01)
        self.assertEqual(view["settled_total_amount"], 2800)
        self.assertEqual(view["reimbursable_now"], 0)
        self.assertEqual(view["disputed_amount"], 2200)

    def test_merchant_view_does_not_disclose_other_merchant_orders(self):
        self.service.confirm_cart("c-a", at=NOV_29_AM, actor="help-a", accept_self_pay=True)
        self.service.confirm_cart("c-b", at="2026-11-29T13:00:00+08:00", actor="help-a", accept_self_pay=True)
        mx = self.service.merchant_view("mx", NOV_29_PM)
        order_ids = {o["order_id"] for o in mx["orders"]}
        self.assertIn("c-b", order_ids)
        self.assertNotIn("c-a", order_ids)


class VoidConflictAndAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.service = QuotaService()
        self.service.upsert_grant(
            {
                "grant": "g1",
                "household": "h",
                "beneficiary": "a",
                "category": "food",
                "amount": 5000,
                "from": "2026-11-01T00:00:00+08:00",
                "until": "2026-12-01T00:00:00+08:00",
            }
        )
        self.service.upsert_terminal_record(
            {
                "terminal": "p1",
                "merchant": "shop",
                "lease": "l1",
                "mandate": None,
                "beneficiary": "a",
                "category": "food",
                "from": "2026-11-01T00:00:00+08:00",
                "until": "2026-11-30T00:00:00+08:00",
                "allocations": {"g1": 3000},
            }
        )

    def test_voided_missing_sequence_preserves_late_conflict(self):
        self.service.submit_receipt(
            {
                "lease": "l1",
                "seq": 1,
                "message": "m1",
                "occurred_at": "2026-11-02T10:00:00+08:00",
                "category": "food",
                "amount": 1000,
            }
        )
        closure = self.service.submit_closure(
            {"terminal": "p1", "lease": "l1", "last_seq": 2, "occurred_at": "2026-11-30T01:00:00+08:00"}
        )
        self.assertEqual(closure["missing_seqs"], [2])
        self.service.void_sequence("l1", 2, "op", "2026-12-01T09:00:00+08:00")
        late = self.service.submit_receipt(
            {
                "lease": "l1",
                "seq": 2,
                "message": "late",
                "occurred_at": "2026-11-03T10:00:00+08:00",
                "category": "food",
                "amount": 500,
            }
        )
        self.assertEqual(late["status"], "conflicts_with_void")
        decision = self.service.sequence_decisions[("l1", 2)]
        self.assertEqual(decision["decision"], "voided")
        self.assertEqual(len(decision["conflicting_receipts"]), 1)
        self.assertEqual(len(self.service.conflicts), 1)

    def test_settled_late_refund_creates_next_adjustment_and_expired_return(self):
        self.service.create_cart(
            {
                "cart": "c1",
                "household": "h",
                "beneficiary": "a",
                "customer": "a",
                "merchant": "shop",
                "items": [{"line": "l1", "category": "food", "amount": 1000}],
            }
        )
        self.service.confirm_cart("c1", at="2026-11-10T10:00:00+08:00")
        delivery = self.service.confirm_delivery(
            "c1", at="2026-11-10T11:00:00+08:00", operator="op"
        )
        old = self.service.issue_settlement(
            merchant="shop", items=[delivery["delivery_id"]], operator="op", at="2026-11-11T10:00:00+08:00"
        )
        refund = self.service.refund_delivery(
            delivery["delivery_id"], at="2026-12-05T10:00:00+08:00", reason="late refund"
        )
        self.assertEqual(refund["refunds"][0]["status"], "refunded_expired")
        self.assertFalse(refund["refunds"][0]["reusable"])
        self.assertEqual(refund["adjustment"]["amount"], -1000)
        self.assertEqual(old["total_amount"], 1000)  # old settlement unchanged

        next_settlement = self.service.issue_settlement(
            merchant="shop", items=[], operator="op2", at="2026-12-06T10:00:00+08:00"
        )
        self.assertEqual(next_settlement["total_amount"], -1000)
        self.assertEqual(next_settlement["adjustment_amount"], -1000)
        summary = self.service.grant_summary("g1", "2026-12-06T10:00:00+08:00")
        # 5000 source - 3000 outstanding test lease hold - 1000 charge.
        # The expired refund does not add the 1000 back.
        self.assertEqual(summary["available_amount"], 1000)
        self.assertEqual(summary["usable_amount"], 0)
        self.assertEqual(summary["expired_refunded"], 1000)


class HttpAdapterTest(unittest.TestCase):
    def test_health_and_generic_action(self):
        service = QuotaService()
        server = create_server(service, "127.0.0.1", 0)
        host, port = server.server_address
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://{host}:{port}/health", timeout=5) as response:
                self.assertEqual(json.loads(response.read()), {"ok": True})
            request = Request(
                f"http://{host}:{port}/v1/actions/grant.upsert",
                data=json.dumps(
                    {
                        "grant": "g",
                        "household": "h",
                        "amount": 100,
                        "from": "2026-11-01T00:00:00+08:00",
                        "until": "2026-12-01T00:00:00+08:00",
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
                self.assertEqual(payload["grant"], "g")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
