"""Executable sample scenario used by the CLI and smoke tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .service import QuotaService

BEFORE_REVOCATION = "2026-11-29T10:00:00+08:00"
SIGN_TIME = "2026-11-29T13:30:00+08:00"
CONFIRM_TIME = "2026-11-29T13:35:00+08:00"
AFTER_REVOCATION = "2026-11-29T15:00:00+08:00"
SETTLE_TIME = "2026-12-01T10:00:00+08:00"


def run_sample_scenario(service: QuotaService, samples: str | Path = "samples") -> dict[str, Any]:
    counts = service.load_ndjson_dir(samples)

    quote_a = service.quote_cart("c-a", BEFORE_REVOCATION)
    order_a = service.confirm_cart(
        "c-a",
        at=BEFORE_REVOCATION,
        actor="help-a",
        accept_self_pay=True,
    )
    delivery_a = service.confirm_delivery(
        "c-a",
        [{"line": "a1", "confirmed_amount": 3100}],
        at=CONFIRM_TIME,
        signed_at=SIGN_TIME,
        actor="help-a",
    )
    service.verify_delivery(delivery_a["delivery_id"], "op-li", SETTLE_TIME)

    blocked = None
    service.create_cart(
        {
            "cart": "c-a-after-revoke",
            "household": "hh8",
            "beneficiary": "res-a",
            "customer": "help-a",
            "merchant": "my",
            "items": [{"line": "late", "category": "fresh", "amount": 100}],
        }
    )
    try:
        service.confirm_cart("c-a-after-revoke", at=AFTER_REVOCATION, actor="help-a", accept_self_pay=True)
    except Exception as exc:  # noqa: BLE001 - report scenario outcome
        blocked = getattr(exc, "to_dict", lambda: {"message": str(exc)})()

    closure = service.submit_closure(
        {"terminal": "pos-4", "lease": "lease1", "last_seq": 4, "occurred_at": "2026-11-30T18:10:00+08:00"}
    )
    missing_after_amended_closure = closure["missing_seqs"]
    void = service.void_sequence("lease1", 4, "op-li", "2026-12-01T09:00:00+08:00")
    conflict_receipt = service.submit_receipt(
        {
            "lease": "lease1",
            "seq": 4,
            "message": "late-t15",
            "occurred_at": "2026-11-29T13:10:00+08:00",
            "category": "fresh",
            "amount": 700,
        }
    )

    service.verify_lease_receipts("lease1", "op-li", "2026-11-30T18:20:00+08:00")
    settlement = service.issue_settlement(
        merchant="mx",
        items=["lease1#1", "lease1#2"],
        operator="op-li",
        at=SETTLE_TIME,
    )
    online_settlement = service.issue_settlement(
        merchant="my",
        items=[delivery_a["delivery_id"]],
        operator="op-li",
        at=SETTLE_TIME,
    )
    merchant = service.merchant_view("mx", SETTLE_TIME)
    resident = service.resident_view("res-a", "hh8", SETTLE_TIME)

    return {
        "loaded": counts,
        "quote_a_self_pay": quote_a["self_pay_amount"],
        "order_a_id": order_a["order_id"],
        "delivery_a": {
            "delivery_id": delivery_a["delivery_id"],
            "actual_amount": delivery_a["actual_amount"],
            "self_refunded_amount": delivery_a["self_refunded_amount"],
        },
        "delegate_after_revocation": blocked,
        "missing_after_amended_closure": missing_after_amended_closure,
        "void_decision": {"lease": void["lease"], "seq": void["seq"], "decision": void["decision"]},
        "late_conflicting_receipt": {
            "lease": conflict_receipt["lease"],
            "seq": conflict_receipt["seq"],
            "status": conflict_receipt["status"],
            "conflicts": conflict_receipt["conflicts"],
        },
        "settlement": {
            "id": settlement["settlement_id"],
            "voucher_amount": settlement["voucher_amount"],
            "total_amount": settlement["total_amount"],
        },
        "online_settlement_my": {
            "id": online_settlement["settlement_id"],
            "total_amount": online_settlement["total_amount"],
        },
        "merchant_mx": {
            "reimbursable_now": merchant["reimbursable_now"],
            "disputed_amount": merchant["disputed_amount"],
            "settled_total_amount": merchant["settled_total_amount"],
            "missing_vouchers": merchant["missing_vouchers"],
        },
        "resident_res_a": {
            "usable_grant_codes": [g["grant"] for g in resident["usable_grants"]],
            "dispute_count": len(resident["disputes"]),
            "waiting_upload": resident["waiting_upload"],
        },
        "conflict_count": len(service.conflicts),
    }
