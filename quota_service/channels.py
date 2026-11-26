"""Role-specific adapters over the same quota service.

These facades do not contain separate business rules; they make the three
integration channels explicit: online ordering, terminal upload and operator
approval.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from .service import QuotaService


class OnlineOrderChannel:
    def __init__(self, service: QuotaService) -> None:
        self.service = service

    def create_cart(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.service.create_cart(record)

    def quote(self, cart_id: str, at: Any = None) -> dict[str, Any]:
        return self.service.quote_cart(cart_id, at)

    def resident_decide(self, quote_id: str, accept: bool, **kwargs: Any) -> dict[str, Any]:
        return self.service.decide_quote(quote_id, accept, **kwargs)

    def confirm(self, cart_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.service.confirm_cart(cart_id, **kwargs)

    def confirm_delivery(self, order_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.service.confirm_delivery(order_id, *args, **kwargs)

    def cancel(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.service.cancel_order(order_id, **kwargs)

    def resident_view(self, beneficiary: str, household: Optional[str] = None, at: Any = None) -> dict[str, Any]:
        return self.service.resident_view(beneficiary, household, at)


class TerminalChannel:
    def __init__(self, service: QuotaService) -> None:
        self.service = service

    def register_lease(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.service.upsert_terminal_record(record)

    def upload_receipt(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.service.submit_receipt(record)

    def submit_closure(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.service.submit_closure(record)

    def merchant_view(self, merchant: str, at: Any = None) -> dict[str, Any]:
        return self.service.merchant_view(merchant, at)


class OperatorChannel:
    def __init__(self, service: QuotaService) -> None:
        self.service = service

    def upsert_source(self, kind: str, record: dict[str, Any]) -> Any:
        return self.service.ingest(kind, record)

    def revoke_mandate(self, mandate: str, revoked_at: Any = None) -> dict[str, Any]:
        return self.service.revoke_mandate(mandate, revoked_at)

    def verify_delivery(self, delivery_id: str, operator: str, at: Any = None) -> dict[str, Any]:
        return self.service.verify_delivery(delivery_id, operator, at)

    def verify_receipt(self, lease: str, seq: int, operator: str, at: Any = None) -> dict[str, Any]:
        return self.service.verify_receipt(lease, seq, operator, at)

    def void_sequence(self, lease: str, seq: int, operator: str, at: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.service.void_sequence(lease, seq, operator, at, **kwargs)

    def void_receipt(self, lease: str, seq: int, operator: str, at: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.service.void_receipt(lease, seq, operator, at, **kwargs)

    def apply_settlement(self, merchant: str, items: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
        return self.service.apply_settlement(merchant, items, **kwargs)

    def issue_settlement(self, settlement_id: str, operator: str, at: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.service.issue_settlement(settlement_id, operator, at, **kwargs)

    def refund_delivery(self, delivery_id: str, at: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.service.refund_delivery(delivery_id, at, **kwargs)

    def admin_view(self) -> dict[str, Any]:
        return self.service.admin_view()
