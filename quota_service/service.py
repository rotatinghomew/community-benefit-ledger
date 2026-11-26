"""Community quota and merchant voucher service.

The service is intentionally framework-free.  The Python API is the core
integration surface; :mod:`quota_service.http_app` and the CLI are thin adapters.
"""
from __future__ import annotations

import copy
import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .errors import (
    AuthorizationError,
    CommitmentExpiredError,
    ConflictError,
    ConfirmationRequiredError,
    FundingError,
    NotFoundError,
    QuotaServiceError,
    ValidationError,
)
from .timeutils import iso, now_dt, optional_text, parse_dt, require_int, within


class QuotaService:
    """In-memory authoritative ledger and workflow service.

    Source records (grants, mandates, terminal leases, receipts, closures and
    carts) can be upserted at any time.  All queries and decisions use the
    currently stored source state.
    """

    def __init__(self, samples_dir: str | Path | None = None, *, data_dir: str | Path | None = None) -> None:
        self.lock = threading.RLock()
        self.grants: dict[str, dict[str, Any]] = {}
        self.mandates: dict[str, dict[str, Any]] = {}
        self.terminals: dict[str, dict[str, Any]] = {}
        self.leases: dict[str, dict[str, Any]] = {}
        self.receipts: dict[tuple[str, int], dict[str, Any]] = {}
        self.receipt_messages: dict[str, tuple[str, int]] = {}
        self.closures: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.sequence_decisions: dict[tuple[str, int], dict[str, Any]] = {}
        self.conflicts: list[dict[str, Any]] = []

        self.carts: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.deliveries: dict[str, dict[str, Any]] = {}

        self.holds: dict[str, dict[str, Any]] = {}
        self.charges: dict[str, dict[str, Any]] = {}

        self.settlements: dict[str, dict[str, Any]] = {}
        self.adjustments: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self._counters: dict[str, int] = defaultdict(int)
        source_dir = samples_dir or data_dir
        if source_dir is not None:
            self.load_ndjson_dir(source_dir)

    @classmethod
    def from_ndjson_dir(cls, path: str | Path) -> "QuotaService":
        return cls(path)

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]}"

    @staticmethod
    def _copy(value: Any) -> Any:
        return copy.deepcopy(value)

    def _event(self, event_type: str, **details: Any) -> dict[str, Any]:
        event = {"id": self._next_id("evt"), "type": event_type, "at": iso(now_dt()), **details}
        self.events.append(event)
        return event

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self._copy(
                {
                    "grants": list(self.grants.values()),
                    "mandates": list(self.mandates.values()),
                    "terminals": list(self.terminals.values()),
                    "leases": list(self.leases.values()),
                    "receipts": list(self.receipts.values()),
                    "closures": [c for rows in self.closures.values() for c in rows],
                    "sequence_decisions": list(self.sequence_decisions.values()),
                    "conflicts": self.conflicts,
                    "carts": list(self.carts.values()),
                    "quotes": list(self.quotes.values()),
                    "orders": list(self.orders.values()),
                    "deliveries": list(self.deliveries.values()),
                    "holds": list(self.holds.values()),
                    "charges": list(self.charges.values()),
                    "settlements": list(self.settlements.values()),
                    "adjustments": list(self.adjustments.values()),
                    "events": self.events,
                }
            )

    def call(self, action: str, payload: Optional[dict[str, Any]] = None, **kwargs: Any) -> Any:
        """Generic action dispatch used by HTTP and CLI adapters."""
        data = dict(payload or {})
        data.update(kwargs)
        handler = getattr(self, f"action_{action.replace('.', '_')}", None)
        if handler is None:
            # Fall back to a direct method for simple CRUD/query actions.
            simple = {
                "grant.upsert": self.upsert_grant,
                "mandate.upsert": self.upsert_mandate,
                "mandate.revoke": self.revoke_mandate,
                "terminal.upsert": self.upsert_terminal_record,
                "lease.upsert": self.upsert_terminal_record,
                "cart.create": self.create_cart,
                "cart.upsert": self.create_cart,
                "cart.quote": self.quote_cart_action,
                "quote.accept": self.accept_quote,
                "quote.reject": self.reject_quote,
                "quote.decide": self.decide_quote,
                "order.confirm": self.confirm_cart,
                "cart.confirm": self.confirm_cart,
                "order.cancel": self.cancel_order,
                "delivery.confirm": self.confirm_delivery,
                "order.deliver": self.confirm_delivery,
                "order.recommit": self.recommit_order,
                "receipt.submit": self.submit_receipt,
                "closure.submit": self.submit_closure,
                "sequence.void": self.void_sequence,
                "receipt.verify": self.verify_receipt,
                "lease.verify": self.verify_lease_receipts,
                "settlement.apply": self.apply_settlement,
                "settlement.issue": self.issue_settlement,
                "delivery.refund": self.refund_delivery,
                "receipt.void": self.void_receipt,
                "view.resident": self.resident_view,
                "view.merchant": self.merchant_view,
                "view.admin": self.admin_view,
                "samples.load": self.load_sample_dir,
                "ndjson.load": self.load_ndjson_dir,
            }.get(action)
            if simple is None:
                raise NotFoundError(f"unknown action: {action}")
            handler = simple
        record_actions = {
            "grant.upsert",
            "mandate.upsert",
            "terminal.upsert",
            "lease.upsert",
            "cart.create",
            "cart.upsert",
            "closure.submit",
        }
        if action in record_actions:
            return handler(data.get("record", data))
        if action == "receipt.submit":
            return self.submit_receipt(data.get("record", data), received_at=data.get("received_at"))
        return handler(**data)

    # ------------------------------------------------------------------
    # Source loading and upserts
    # ------------------------------------------------------------------
    def load_ndjson_dir(self, path: str | Path) -> dict[str, int]:
        root = Path(path)
        order = ["grants", "mandates", "terminals", "receipts", "closures", "carts"]
        counts: dict[str, int] = {}
        with self.lock:
            for name in order:
                file_path = root / f"{name}.ndjson"
                if not file_path.exists():
                    continue
                count = 0
                for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValidationError(
                            f"invalid JSON in {file_path.name}:{line_no}", details={"line": line_no}
                        ) from exc
                    self.ingest(name, record)
                    count += 1
                counts[name] = count
        return counts

    # Backwards-compatible descriptive alias.
    load_sample_dir = load_ndjson_dir

    def ingest(self, kind: str, record: dict[str, Any]) -> Any:
        if not isinstance(record, dict):
            raise ValidationError("source record must be a JSON object")
        kind = kind.removesuffix(".ndjson")
        if kind == "grants":
            return self.upsert_grant(record)
        if kind == "mandates":
            return self.upsert_mandate(record)
        if kind in {"terminals", "leases"}:
            return self.upsert_terminal_record(record)
        if kind == "receipts":
            return self.submit_receipt(record)
        if kind == "closures":
            return self.submit_closure(record)
        if kind == "carts":
            return self.create_cart(record)
        raise NotFoundError(f"unknown source kind: {kind}")

    @staticmethod
    def _grant_key(record: dict[str, Any]) -> str:
        code = record.get("grant") or record.get("code") or record.get("id")
        if not code:
            raise ValidationError("grant is required")
        return str(code)

    def upsert_grant(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            code = self._grant_key(record)
            amount = require_int(record.get("amount"), field="amount", minimum=1)
            start = parse_dt(record.get("from"), field="from")
            end = parse_dt(record.get("until"), field="until")
            if not start < end:
                raise ValidationError("grant half-open period must have from < until")
            normalized = {
                "grant": code,
                "household": str(record.get("household")),
                "beneficiary": optional_text(record.get("beneficiary")),
                "category": optional_text(record.get("category")),
                "amount": amount,
                "from": iso(start),
                "until": iso(end),
            }
            if not normalized["household"] or normalized["household"] == "None":
                raise ValidationError("grant household is required")
            self.grants[code] = normalized
            # A grant can arrive after a lease source; reconcile its holds.
            for lease in list(self.leases.values()):
                if code in lease.get("allocations", {}):
                    self._ensure_lease_hold(lease["lease"], code)
                    self._validate_lease_sources(lease)
            self._event("grant.upserted", grant=code)
            return self._copy(normalized)

    def upsert_mandate(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            mid = record.get("mandate") or record.get("id")
            if not mid:
                raise ValidationError("mandate is required")
            mid = str(mid)
            start = parse_dt(record.get("from"), field="from")
            end = parse_dt(record.get("until"), field="until")
            if not start < end:
                raise ValidationError("mandate half-open period must have from < until")
            revoked_at = record.get("revoked_at")
            revoked = parse_dt(revoked_at, field="revoked_at") if revoked_at else None
            normalized = {
                "mandate": mid,
                "beneficiary": str(record.get("beneficiary")),
                "delegate": str(record.get("delegate")),
                "from": iso(start),
                "until": iso(end),
                "revoked_at": iso(revoked),
            }
            self.mandates[mid] = normalized
            for lease in self.leases.values():
                if lease.get("mandate") == mid:
                    self._validate_lease_sources(lease)
            self._event("mandate.upserted", mandate=mid, revoked_at=normalized["revoked_at"])
            return self._copy(normalized)

    def revoke_mandate(self, mandate: str, revoked_at: Any = None) -> dict[str, Any]:
        with self.lock:
            if mandate not in self.mandates:
                raise NotFoundError(f"mandate not found: {mandate}")
            at = parse_dt(revoked_at) if revoked_at else now_dt()
            self.mandates[mandate]["revoked_at"] = iso(at)
            self._event("mandate.revoked", mandate=mandate, revoked_at=iso(at))
            return self._copy(self.mandates[mandate])

    def upsert_terminal_record(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            terminal_id = record.get("terminal") or record.get("terminal_id")
            lease_id = record.get("lease") or record.get("lease_id")
            merchant = record.get("merchant")
            if not terminal_id or not lease_id or not merchant:
                raise ValidationError("terminal, lease and merchant are required")
            terminal_id, lease_id, merchant = str(terminal_id), str(lease_id), str(merchant)
            start = parse_dt(record.get("from"), field="from")
            end = parse_dt(record.get("until"), field="until")
            if not start < end:
                raise ValidationError("lease half-open period must have from < until")
            allocations = record.get("allocations") or {}
            if not isinstance(allocations, dict) or not allocations:
                raise ValidationError("lease allocations must be a non-empty object")
            norm_allocations: dict[str, int] = {}
            for code, amount in allocations.items():
                norm_allocations[str(code)] = require_int(amount, field=f"allocations.{code}", minimum=0)
            if sum(norm_allocations.values()) <= 0:
                raise ValidationError("lease allocation total must be positive")

            prior_terminal = self.terminals.get(terminal_id)
            if prior_terminal and prior_terminal["merchant"] != merchant:
                raise ConflictError(
                    "terminal is registered to another merchant",
                    details={"terminal": terminal_id, "merchant": prior_terminal["merchant"]},
                )
            self.terminals[terminal_id] = {"terminal": terminal_id, "merchant": merchant}

            lease = {
                "lease": lease_id,
                "terminal": terminal_id,
                "merchant": merchant,
                "mandate": optional_text(record.get("mandate")),
                "beneficiary": str(record.get("beneficiary")),
                "category": optional_text(record.get("category")),
                "from": iso(start),
                "until": iso(end),
                "allocations": norm_allocations,
            }
            self.leases[lease_id] = lease
            for code, amount in norm_allocations.items():
                self._ensure_lease_hold(lease_id, code, amount)
            self._validate_lease_sources(lease)
            self._event("lease.upserted", lease=lease_id, terminal=terminal_id)
            # Receipts may have arrived before their lease source.
            for key, receipt in list(self.receipts.items()):
                if key[0] == lease_id and receipt.get("status") == "waiting_for_lease":
                    self._evaluate_and_store_receipt(receipt)
            return self._copy(lease)

    def _ensure_lease_hold(self, lease_id: str, grant_code: str, desired_amount: Optional[int] = None) -> dict[str, Any]:
        lease = self.leases[lease_id]
        amount = desired_amount if desired_amount is not None else lease["allocations"].get(grant_code, 0)
        hold_id = f"hold:lease:{lease_id}:{grant_code}"
        hold = self.holds.get(hold_id)
        if hold is None:
            grant = self.grants.get(grant_code)
            if grant is None:
                # Sources may be provided in any order.  The hold is created when
                # the grant arrives; the lease itself is still stored.
                return {"id": hold_id, "grant": grant_code, "pending_source": True, "amount": amount}
            if amount > grant["amount"]:
                raise ValidationError(
                    "lease allocation exceeds grant source amount",
                    details={"lease": lease_id, "grant": grant_code},
                )
            hold = self._create_hold(
                grant_code,
                amount,
                source_type="lease",
                source_id=lease_id,
                hold_id=hold_id,
                effective_from=lease["from"],
                allocation=grant_code,
            )
        else:
            used = hold.get("used_amount", 0)
            if amount < used:
                raise ConflictError(
                    "updated lease allocation is below amount already captured",
                    details={"hold": hold_id, "used": used, "requested": amount},
                )
            hold["amount"] = amount
        return hold

    # ------------------------------------------------------------------
    # Ledger and balances
    # ------------------------------------------------------------------
    def _validate_lease_sources(self, lease: dict[str, Any]) -> None:
        """Validate the protocol's one terminal/beneficiary/mandate/category rule."""
        if lease.get("mandate"):
            mandate = self.mandates.get(lease["mandate"])
            if mandate is not None and mandate["beneficiary"] != lease["beneficiary"]:
                raise ValidationError(
                    "lease mandate beneficiary does not match lease beneficiary",
                    details={"lease": lease["lease"], "mandate": lease["mandate"]},
                )
        households: set[str] = set()
        for code in lease.get("allocations", {}):
            grant = self.grants.get(code)
            if grant is None:
                continue
            households.add(grant["household"])
            if grant.get("beneficiary") is not None and grant["beneficiary"] != lease["beneficiary"]:
                raise ValidationError(
                    "lease allocation belongs to another beneficiary",
                    details={"lease": lease["lease"], "grant": code},
                )
            if grant.get("category") is not None and lease.get("category") is not None and grant["category"] != lease["category"]:
                raise ValidationError(
                    "lease allocation category does not match lease category",
                    details={"lease": lease["lease"], "grant": code},
                )
        if len(households) > 1:
            raise ValidationError("lease allocations must belong to one household")

    def _hold_remaining(self, hold: dict[str, Any], at: Any = None) -> int:
        if at is None:
            return max(
                0,
                hold["amount"]
                - hold.get("used_amount", 0)
                - hold.get("released_amount", 0)
                - hold.get("expired_amount", 0),
            )
        when = parse_dt(at)
        effective_from = hold.get("effective_from")
        if effective_from and when < parse_dt(effective_from):
            return 0

        def event_sum(name: str) -> int:
            total = 0
            for event in hold.get(name, []):
                if parse_dt(event["at"]) <= when:
                    total += int(event["amount"])
            return total

        used = event_sum("usage_events")
        released = event_sum("release_events")
        expired = event_sum("expiry_events")
        return max(0, hold["amount"] - used - released - expired)

    def _create_hold(
        self,
        grant_code: str,
        amount: int,
        *,
        source_type: str,
        source_id: str,
        line_id: Optional[str] = None,
        hold_id: Optional[str] = None,
        effective_from: Any = None,
        allocation: Optional[str] = None,
    ) -> dict[str, Any]:
        hid = hold_id or self._next_id("hold")
        if hid in self.holds:
            return self.holds[hid]
        at = parse_dt(effective_from) if effective_from else now_dt()
        hold = {
            "id": hid,
            "grant": grant_code,
            "amount": amount,
            "used_amount": 0,
            "released_amount": 0,
            "expired_amount": 0,
            "usage_events": [],
            "release_events": [],
            "expiry_events": [],
            "effective_from": iso(at),
            "source_type": source_type,
            "source_id": source_id,
            "line_id": line_id,
            "allocation": allocation,
            "status": "held",
        }
        self.holds[hid] = hold
        return hold

    def _use_hold(self, hold_id: str, amount: int, at: Any = None, *, reason: Optional[str] = None) -> None:
        hold = self.holds[hold_id]
        when = parse_dt(at) if at else now_dt()
        if amount < 0 or amount > self._hold_remaining(hold, when):
            raise FundingError("hold does not have enough remaining amount", details={"hold": hold_id, "amount": amount})
        hold["used_amount"] = hold.get("used_amount", 0) + amount
        hold.setdefault("usage_events", []).append(
            {"amount": amount, "at": iso(when), "reason": reason or "captured"}
        )

    def _release_hold(self, hold_id: str, amount: int, at: Any = None, *, reason: str = "released") -> None:
        hold = self.holds[hold_id]
        when = parse_dt(at) if at else now_dt()
        releasable = self._hold_remaining(hold, when)
        amount = min(amount, releasable)
        if amount <= 0:
            return
        hold["released_amount"] = hold.get("released_amount", 0) + amount
        hold.setdefault("release_events", []).append({"amount": amount, "at": iso(when), "reason": reason})

    def _expire_hold(self, hold_id: str, amount: int, at: Any = None, *, reason: str) -> None:
        hold = self.holds[hold_id]
        when = parse_dt(at) if at else now_dt()
        amount = min(amount, self._hold_remaining(hold, when))
        if amount <= 0:
            return
        hold["expired_amount"] = hold.get("expired_amount", 0) + amount
        hold.setdefault("expiry_events", []).append({"amount": amount, "at": iso(when), "reason": reason})

    def expire_due_commitments(self, at: Any = None) -> list[dict[str, Any]]:
        """Expire remaining holds at the exact grant-period boundary.

        Returns the newly expired hold amounts.  Late vouchers whose actual
        occurrence was inside the period can still be captured with an
        occurrence-effective event after this sweep.
        """
        with self.lock:
            when = parse_dt(at) if at else now_dt()
            expired: list[dict[str, Any]] = []
            for hold in list(self.holds.values()):
                grant = self.grants.get(hold["grant"])
                if grant is None:
                    continue
                until = parse_dt(grant["until"])
                if when < until:
                    continue
                remaining = self._hold_remaining(hold, until)
                if remaining <= 0:
                    continue
                self._expire_hold(hold["id"], remaining, until, reason="grant_period_ended")
                expired.append({"hold": hold["id"], "grant": hold["grant"], "amount": remaining, "at": grant["until"]})
            return expired

    def _set_release_target(self, hold_id: str, target_released: int, at: Any, *, reason: str) -> None:
        hold = self.holds[hold_id]
        when = parse_dt(at)
        current = hold.get("released_amount", 0)
        delta = target_released - current
        if delta == 0:
            return
        hold["released_amount"] = target_released
        hold.setdefault("release_events", []).append({"amount": delta, "at": iso(when), "reason": reason})

    def _create_charge(
        self,
        grant_code: str,
        amount: int,
        *,
        source_type: str,
        source_id: str,
        line_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        effective_at: Any = None,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValidationError("charge amount must be positive")
        effective = parse_dt(effective_at) if effective_at else now_dt()
        charge = {
            "id": self._next_id("ch"),
            "grant": grant_code,
            "amount": amount,
            "source_type": source_type,
            "source_id": source_id,
            "line_id": line_id,
            "created_at": iso(now_dt()),
            "effective_at": iso(effective),
            "refunds": [],
            "metadata": metadata or {},
        }
        self.charges[charge["id"]] = charge
        return charge

    def _refund_charge(
        self,
        charge_id: str,
        amount: int,
        at: Any,
        *,
        reason: str,
        source_type: str = "refund",
        source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        charge = self.charges[charge_id]
        already = sum(r["amount"] for r in charge["refunds"])
        if amount <= 0 or amount > charge["amount"] - already:
            raise ValidationError("refund amount exceeds refundable charge", details={"charge": charge_id})
        when = parse_dt(at) if at else now_dt()
        grant = self.grants.get(charge["grant"])
        reusable = bool(grant and within(when, grant["from"], grant["until"]))
        if reusable:
            status = "refunded_available"
            unusable_reason = None
        else:
            status = "refunded_expired"
            if grant is None:
                unusable_reason = "grant_source_missing"
            elif when >= parse_dt(grant["until"]):
                unusable_reason = "grant_period_ended"
            else:
                unusable_reason = "grant_period_not_started"
        refund = {
            "id": self._next_id("rf"),
            "charge": charge_id,
            "grant": charge["grant"],
            "amount": amount,
            "at": iso(when),
            "reason": reason,
            "reusable": reusable,
            "status": status,
            "unusable_reason": unusable_reason,
            "source_type": source_type,
            "source_id": source_id,
        }
        charge["refunds"].append(refund)
        self._event("charge.refunded", charge=charge_id, refund=refund["id"], reusable=reusable)
        return refund

    def grant_summary(
        self,
        grant_code: str,
        at: Any = None,
        *,
        exclude_source_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            grant = self.grants.get(grant_code)
            if grant is None:
                raise NotFoundError(f"grant not found: {grant_code}")
            when = parse_dt(at) if at else now_dt()
            holds = [h for h in self.holds.values() if h["grant"] == grant_code]
            held_remaining = 0
            expired_held = 0
            for hold in holds:
                if exclude_source_id and hold.get("source_id") == exclude_source_id:
                    continue
                held_remaining += self._hold_remaining(hold, when)
                expired_held += sum(
                    event["amount"]
                    for event in hold.get("expiry_events", [])
                    if parse_dt(event["at"]) <= when
                )
            charged_gross = 0
            reusable_refunded = 0
            expired_refunded = 0
            for charge in self.charges.values():
                if charge["grant"] != grant_code:
                    continue
                if parse_dt(charge.get("effective_at") or charge["created_at"]) > when:
                    continue
                if exclude_source_id and charge.get("source_id") == exclude_source_id:
                    # Re-quoting a live order before capture only has holds.
                    continue
                charged_gross += charge["amount"]
                for refund in charge["refunds"]:
                    if parse_dt(refund["at"]) > when:
                        continue
                    if refund.get("reusable"):
                        reusable_refunded += refund["amount"]
                    else:
                        expired_refunded += refund["amount"]
            available = grant["amount"] - held_remaining - charged_gross + reusable_refunded
            active = within(when, grant["from"], grant["until"])
            return {
                "grant": grant_code,
                "household": grant["household"],
                "beneficiary": grant["beneficiary"],
                "category": grant["category"],
                "amount": grant["amount"],
                "from": grant["from"],
                "until": grant["until"],
                "active_at": iso(when),
                "usable_now": active,
                "held_remaining": held_remaining,
                "expired_held_amount": expired_held,
                "charged_gross": charged_gross,
                "reusable_refunded": reusable_refunded,
                "expired_refunded": expired_refunded,
                "available_amount": available,
                "usable_amount": max(0, available) if active else 0,
            }

    def _balance(self, grant_code: str, at: Any, exclude_source_id: Optional[str] = None) -> int:
        return self.grant_summary(grant_code, at, exclude_source_id=exclude_source_id)["available_amount"]

    def _eligible_grants(
        self,
        household: str,
        beneficiary: str,
        category: Optional[str],
        at: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        when = parse_dt(at) if at else now_dt()
        resident: list[dict[str, Any]] = []
        shared: list[dict[str, Any]] = []
        for grant in self.grants.values():
            if grant["household"] != household:
                continue
            if grant["beneficiary"] is not None and grant["beneficiary"] != beneficiary:
                continue
            if grant["category"] is not None and category is not None and grant["category"] != category:
                continue
            if not within(when, grant["from"], grant["until"]):
                continue
            if grant["beneficiary"] is None:
                shared.append(grant)
            else:
                resident.append(grant)
        key = lambda g: (parse_dt(g["until"]), g["grant"])  # noqa: E731
        resident.sort(key=key)
        shared.sort(key=key)
        return resident, shared

    # ------------------------------------------------------------------
    # Carts, quotes and online orders
    # ------------------------------------------------------------------
    def create_cart(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            cart_id = record.get("cart") or record.get("cart_id") or record.get("id")
            if not cart_id:
                raise ValidationError("cart is required")
            cart_id = str(cart_id)
            items = record.get("items")
            if not isinstance(items, list) or not items:
                raise ValidationError("cart items must be a non-empty list")
            norm_items = []
            seen_lines: set[str] = set()
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationError("each cart item must be an object")
                line = str(item.get("line") or item.get("line_id") or f"{index + 1}")
                if line in seen_lines:
                    raise ValidationError(f"duplicate line id: {line}")
                seen_lines.add(line)
                category = optional_text(item.get("category"))
                if category is None:
                    raise ValidationError(f"item {line} category is required")
                amount = require_int(item.get("amount"), field=f"items.{line}.amount", minimum=0)
                norm_items.append(
                    {
                        "line": line,
                        "category": category,
                        "amount": amount,
                        "description": item.get("description"),
                    }
                )
            prior_cart = self.carts.get(cart_id)
            cart = {
                "cart": cart_id,
                "household": str(record.get("household")),
                "beneficiary": str(record.get("beneficiary")),
                "customer": str(record.get("customer")),
                "merchant": optional_text(record.get("merchant")),
                "items": norm_items,
                "status": record.get("status", prior_cart.get("status", "draft") if prior_cart else "draft"),
                "latest_quote_id": prior_cart.get("latest_quote_id") if prior_cart else None,
                "order_id": prior_cart.get("order_id") if prior_cart else None,
                "accepted_self_pay_amount": prior_cart.get("accepted_self_pay_amount", 0) if prior_cart else 0,
                "resident_rejected_quote_id": prior_cart.get("resident_rejected_quote_id") if prior_cart else None,
                "resident_rejected_self_pay_amount": prior_cart.get("resident_rejected_self_pay_amount") if prior_cart else None,
            }
            if not cart["household"] or not cart["beneficiary"] or not cart["customer"]:
                raise ValidationError("cart household, beneficiary and customer are required")
            self.carts[cart_id] = cart
            self._event("cart.upserted", cart=cart_id)
            return self._copy(cart)

    def quote_cart_action(self, **kwargs: Any) -> dict[str, Any]:
        cart_id = kwargs.get("cart_id") or kwargs.get("cart") or kwargs.get("id")
        return self.quote_cart(str(cart_id), at=kwargs.get("at"))

    def quote_cart(self, cart: Any, at: Any = None, *, prior_quote_id: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            if isinstance(cart, dict):
                stored = self.create_cart(cart)
                cart_id = stored["cart"]
            else:
                cart_id = str(cart)
            cart_record = self._get_cart(cart_id)
            when = parse_dt(at) if at else now_dt()
            self.expire_due_commitments(when)
            quote = self._build_quote(cart_record, when)
            previous_id = prior_quote_id or cart_record.get("latest_quote_id")
            previous = self.quotes.get(previous_id) if previous_id else None
            previous_self = int(previous["self_pay_amount"]) if previous else int(cart_record.get("accepted_self_pay_amount", 0))
            increase = max(0, quote["self_pay_amount"] - previous_self)
            rejected_self = cart_record.get("resident_rejected_self_pay_amount")
            rejected_higher_or_equal = (
                rejected_self is not None
                and quote["self_pay_amount"] >= int(rejected_self)
                and quote["self_pay_amount"] > int(cart_record.get("accepted_self_pay_amount", 0))
            )
            requires_acceptance = bool((previous is not None and increase > 0) or rejected_higher_or_equal)
            quote.update(
                {
                    "id": self._next_id("q"),
                    "status": "offered",
                    "revision": previous is not None,
                    "previous_quote_id": previous_id if previous else None,
                    "previous_self_pay_amount": previous_self,
                    "self_pay_increase": increase,
                    "requires_resident_acceptance": requires_acceptance,
                }
            )
            self.quotes[quote["id"]] = quote
            cart_record["latest_quote_id"] = quote["id"]
            return self._copy(quote)

    def _get_cart(self, cart_id: str) -> dict[str, Any]:
        cart = self.carts.get(str(cart_id))
        if cart is None:
            raise NotFoundError(f"cart not found: {cart_id}")
        return cart

    def _build_quote(self, cart: dict[str, Any], when: Any, *, exclude_source_id: Optional[str] = None) -> dict[str, Any]:
        household = cart["household"]
        beneficiary = cart["beneficiary"]
        balances: dict[str, int] = {}
        lines: list[dict[str, Any]] = []
        total = 0
        grant_total = 0
        self_total = 0
        expires: list[str] = []

        for item in cart["items"]:
            line_id = item["line"]
            category = item["category"]
            amount = item["amount"]
            total += amount
            remaining = amount
            funding: list[dict[str, Any]] = []
            resident, shared = self._eligible_grants(household, beneficiary, category, when)
            for scope, grants in (("resident_specific", resident), ("household_shared", shared)):
                for grant in grants:
                    code = grant["grant"]
                    if code not in balances:
                        summary = self.grant_summary(code, when, exclude_source_id=exclude_source_id)
                        # A negative accounting balance is not spendable.
                        balances[code] = max(0, summary["available_amount"])
                    take = min(remaining, balances[code])
                    if take <= 0:
                        continue
                    balances[code] -= take
                    remaining -= take
                    expires.append(grant["until"])
                    part = {
                        "type": "grant",
                        "kind": scope,
                        "grant": code,
                        "amount": take,
                        "expires_at": grant["until"],
                        "until": grant["until"],
                        "expiration_date": grant["until"].split("T", 1)[0],
                        "period_from": grant["from"],
                        "period_until": grant["until"],
                        "beneficiary": grant["beneficiary"],
                        "category": grant["category"],
                    }
                    funding.append(part)
                    grant_total += take
                    if remaining == 0:
                        break
                if remaining == 0:
                    break
            self_pay = remaining
            if self_pay > 0:
                funding.append({"type": "self_pay", "kind": "self_pay", "grant": None, "amount": self_pay, "expires_at": None, "until": None, "expiration_date": None})
                self_total += self_pay
            lines.append(
                {
                    "line": line_id,
                    "category": category,
                    "amount": amount,
                    "grant_amount": amount - self_pay,
                    "self_pay_amount": self_pay,
                    "funding": funding,
                }
            )
        return {
            "cart": cart["cart"],
            "household": household,
            "beneficiary": beneficiary,
            "quoted_at": iso(when),
            "total_amount": total,
            "grant_amount": grant_total,
            "self_pay_amount": self_total,
            "earliest_expires_at": min(expires) if expires else None,
            "lines": lines,
        }

    def reject_quote(self, quote_id: str, reason: str = "resident_rejected") -> dict[str, Any]:
        with self.lock:
            quote = self.quotes.get(quote_id)
            if quote is None:
                raise NotFoundError(f"quote not found: {quote_id}")
            quote["status"] = "rejected"
            quote["resident_decision"] = "rejected"
            quote["rejection_reason"] = reason
            cart = self.carts.get(quote["cart"])
            if cart is not None:
                cart["resident_rejected_quote_id"] = quote_id
                cart["resident_rejected_self_pay_amount"] = quote["self_pay_amount"]
            self._event("quote.rejected", quote=quote_id, reason=reason)
            return self._copy(quote)

    def accept_quote(self, quote_id: str, resident: Optional[str] = None) -> dict[str, Any]:
        with self.lock:
            quote = self.quotes.get(quote_id)
            if quote is None:
                raise NotFoundError(f"quote not found: {quote_id}")
            if quote.get("status") == "rejected":
                raise ConflictError("rejected quote cannot be accepted")
            quote["resident_decision"] = "accepted"
            quote["resident"] = resident
            self._event("quote.accepted", quote=quote_id, resident=resident)
            return self._copy(quote)

    def decide_quote(self, quote_id: str, accept: bool, *, resident: Optional[str] = None, reason: str = "resident_decision") -> dict[str, Any]:
        return self.accept_quote(quote_id, resident) if accept else self.reject_quote(quote_id, reason)

    def find_mandate(
        self,
        actor: str,
        beneficiary: str,
        at: Any,
        mandate_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        when = parse_dt(at) if at else now_dt()
        if mandate_id:
            mandate = self.mandates.get(str(mandate_id))
            if mandate is None:
                raise NotFoundError(f"mandate not found: {mandate_id}")
            if mandate["delegate"] != actor or mandate["beneficiary"] != beneficiary:
                raise AuthorizationError("mandate does not cover this resident/delegate")
            if not self._mandate_active(mandate, when):
                raise AuthorizationError(
                    "mandate is not active at the requested time",
                    details={"mandate": mandate["mandate"], "revoked_at": mandate.get("revoked_at")},
                )
            return mandate
        candidates = [
            m
            for m in self.mandates.values()
            if m["delegate"] == actor and m["beneficiary"] == beneficiary and self._mandate_active(m, when)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda m: parse_dt(m["from"]), reverse=True)
        return candidates[0]

    @staticmethod
    def _mandate_active(mandate: dict[str, Any], when: Any) -> bool:
        t = parse_dt(when)
        if not within(t, mandate["from"], mandate["until"]):
            return False
        revoked_at = mandate.get("revoked_at")
        return not revoked_at or t < parse_dt(revoked_at)

    def _authorize_actor(
        self,
        actor: Optional[str],
        cart: dict[str, Any],
        when: Any,
        mandate_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        actor = str(actor or cart["customer"])
        if actor == cart["beneficiary"]:
            return None
        mandate = self.find_mandate(actor, cart["beneficiary"], when, mandate_id)
        if mandate is None:
            matching = [
                m
                for m in self.mandates.values()
                if m["delegate"] == actor and m["beneficiary"] == cart["beneficiary"]
            ]
            revoked = next(
                (
                    m
                    for m in matching
                    if m.get("revoked_at") and not when < parse_dt(m["revoked_at"])
                ),
                None,
            )
            if revoked:
                raise AuthorizationError(
                    "mandate was revoked before this confirmation",
                    code="mandate_revoked",
                    details={
                        "actor": actor,
                        "beneficiary": cart["beneficiary"],
                        "mandate": revoked["mandate"],
                        "revoked_at": revoked["revoked_at"],
                        "at": iso(when),
                    },
                )
            raise AuthorizationError(
                "actor is not the beneficiary and has no active mandate",
                code="mandate_inactive",
                details={"actor": actor, "beneficiary": cart["beneficiary"]},
            )
        return mandate

    def confirm_cart(
        self,
        cart_or_id: Any = None,
        actor: Optional[str] = None,
        at: Any = None,
        *,
        cart_id: Optional[str] = None,
        cart: Optional[dict[str, Any]] = None,
        quote_id: Optional[str] = None,
        accept_self_pay: bool = False,
        resident_accepts_increase: bool = False,
        mandate_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            record = cart if cart is not None else (cart_or_id if isinstance(cart_or_id, dict) else None)
            if record is not None:
                cart_id = self.create_cart(record)["cart"]
            elif cart_id is None:
                cart_id = str(cart_or_id)
            else:
                cart_id = str(cart_id)
            cart_record = self._get_cart(cart_id)
            cart = cart_record
            when = parse_dt(at) if at else now_dt()
            if cart_record.get("order_id") and self.orders.get(cart_record["order_id"]):
                # Confirmation is idempotent for an already created order.
                return self._copy(self.orders[cart_record["order_id"]])

            if quote_id:
                quote = self.quotes.get(quote_id)
                if quote is None or quote["cart"] != cart_id:
                    raise NotFoundError("quote not found for cart")
                if quote.get("status") == "rejected":
                    raise ConfirmationRequiredError("quote was rejected by the resident")
                if self._fingerprint(quote["lines"]) != self._fingerprint(self._build_quote(cart_record, when)["lines"]):
                    raise ConflictError("cart changed after quote; obtain a new quote")
            else:
                quote = self.quote_cart(cart_id, when)

            prior_self = int(cart_record.get("accepted_self_pay_amount", 0))
            if quote["self_pay_amount"] > 0 and not accept_self_pay:
                raise ConfirmationRequiredError(
                    "explicit self-pay confirmation is required",
                    details={"quote_id": quote["id"], "self_pay_amount": quote["self_pay_amount"]},
                )
            increase_required = bool(
                quote.get("requires_resident_acceptance")
                or (prior_self > 0 and quote["self_pay_amount"] > prior_self)
            )
            if increase_required and not resident_accepts_increase:
                raise ConfirmationRequiredError(
                    "self-pay increased; resident must accept the revised basket",
                    details={
                        "quote_id": quote["id"],
                        "previous_self_pay_amount": quote.get("previous_self_pay_amount", prior_self),
                        "self_pay_amount": quote["self_pay_amount"],
                        "increase": quote.get("self_pay_increase", quote["self_pay_amount"] - prior_self),
                    },
                )

            mandate = self._authorize_actor(actor, cart, when, mandate_id)
            order_id = str(cart_id)
            order = {
                "order_id": order_id,
                "cart": cart_id,
                "household": cart["household"],
                "beneficiary": cart["beneficiary"],
                "merchant": cart.get("merchant"),
                "customer": cart["customer"],
                "confirmed_by": str(actor or cart["customer"]),
                "mandate": mandate["mandate"] if mandate else None,
                "confirmed_at": iso(when),
                "quote_id": quote["id"],
                "total_amount": quote["total_amount"],
                "grant_amount": quote["grant_amount"],
                "self_pay_amount": quote["self_pay_amount"],
                "self_refunded_amount": 0,
                "status": "awaiting_delivery",
                "lines": [],
                "delivery_ids": [],
                "idempotency_key": idempotency_key,
            }

            for line in quote["lines"]:
                order_line = self._copy(line)
                for part in order_line["funding"]:
                    if part["type"] != "grant":
                        part["hold_id"] = None
                        part["charge_id"] = None
                        continue
                    hold_id = self._next_id("hold")
                    # Re-check immediately before creating the hold.
                    available = self._balance(part["grant"], when)
                    if available < part["amount"]:
                        raise FundingError(
                            "quota is no longer available for confirmation",
                            details={"grant": part["grant"], "available": available, "required": part["amount"]},
                        )
                    self._create_hold(
                        part["grant"],
                        part["amount"],
                        source_type="order",
                        source_id=order_id,
                        line_id=line["line"],
                        hold_id=hold_id,
                        effective_from=when,
                    )
                    part["hold_id"] = hold_id
                    part["charge_id"] = None
                order["lines"].append(order_line)

            self.orders[order_id] = order
            cart["order_id"] = order_id
            cart["status"] = "confirmed"
            cart["accepted_self_pay_amount"] = quote["self_pay_amount"]
            self.quotes[quote["id"]]["status"] = "accepted"
            self._event("order.confirmed", order=order_id, quote=quote["id"])
            return self._copy(order)

    @staticmethod
    def _fingerprint(lines: Sequence[dict[str, Any]]) -> str:
        return json.dumps(
            [{"line": x["line"], "category": x["category"], "amount": x["amount"]} for x in lines],
            sort_keys=True,
            ensure_ascii=False,
        )

    def revise_cart(self, cart_id: str, items: list[dict[str, Any]], at: Any = None) -> dict[str, Any]:
        """Replace a draft/unconfirmed cart basket and return a fresh quote."""
        with self.lock:
            cart = self._get_cart(cart_id)
            if cart.get("order_id"):
                raise ConflictError("confirmed order cannot be revised in place; use delivery/recommit workflow")
            previous_quote_id = cart.get("latest_quote_id")
            previous_self = cart.get("accepted_self_pay_amount", 0)
            updated = self.create_cart({**cart, "items": items, "status": "draft"})
            self.carts[cart_id]["latest_quote_id"] = previous_quote_id
            self.carts[cart_id]["accepted_self_pay_amount"] = previous_self
            return self.quote_cart(updated["cart"], at)

    def recommit_order(
        self,
        order_id: str,
        at: Any = None,
        *,
        actor: Optional[str] = None,
        accept_self_pay: bool = False,
        resident_accepts_increase: bool = False,
        mandate_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Re-obtain a funding promise after old grants reached period end."""
        with self.lock:
            order = self._get_order(order_id)
            when = parse_dt(at) if at else now_dt()
            # Expire any untouched old holds.  Captured/delivered lines remain.
            for line in order["lines"]:
                for part in line["funding"]:
                    hold_id = part.get("hold_id")
                    if part["type"] != "grant" or not hold_id:
                        continue
                    grant = self.grants[part["grant"]]
                    hold = self.holds[hold_id]
                    remaining = self._hold_remaining(hold, when)
                    if remaining > 0 and not within(when, grant["from"], grant["until"]):
                        self._expire_hold(hold_id, remaining, when, reason="grant_period_ended")
                        part["commitment_status"] = "expired_period_ended"
            # Build a cart-shaped object from undelivered full line amounts.
            pending_items = []
            for line in order["lines"]:
                delivered = line.get("delivered_amount", 0)
                pending = line["amount"] - delivered
                if pending > 0:
                    pending_items.append({"line": line["line"], "category": line["category"], "amount": pending})
            if not pending_items:
                raise ConflictError("order has no pending amount to recommit")
            cart_view = {
                "cart": order["cart"],
                "household": order["household"],
                "beneficiary": order["beneficiary"],
                "customer": order["customer"],
                "items": pending_items,
            }
            quote = self._build_quote(cart_view, when)
            quote["id"] = self._next_id("q")
            prior_self = order.get("self_pay_amount", 0) - order.get("self_refunded_amount", 0)
            if quote["self_pay_amount"] > 0 and not accept_self_pay:
                raise ConfirmationRequiredError("new commitment requires self-pay confirmation", details=quote)
            if quote["self_pay_amount"] > prior_self and not resident_accepts_increase:
                raise ConfirmationRequiredError("new commitment increases self-pay; resident acceptance required")
            self._authorize_actor(actor or order["confirmed_by"], self.carts[order["cart"]], when, mandate_id)
            # Append replacement funding parts to pending lines.
            for qline in quote["lines"]:
                target = next(x for x in order["lines"] if x["line"] == qline["line"])
                target.setdefault("recommitments", []).append({"quote_id": quote["id"], "funding": qline["funding"]})
                for part in qline["funding"]:
                    if part["type"] != "grant":
                        continue
                    hold_id = self._next_id("hold")
                    self._create_hold(
                        part["grant"],
                        part["amount"],
                        source_type="order_recommitment",
                        source_id=order_id,
                        line_id=qline["line"],
                        hold_id=hold_id,
                        effective_from=when,
                    )
                    part["hold_id"] = hold_id
            order["recommit_quote_id"] = quote["id"]
            order["status"] = "awaiting_delivery"
            self._event("order.recommitted", order=order_id, quote=quote["id"])
            return self._copy(order)

    def _get_order(self, order_id: str) -> dict[str, Any]:
        order = self.orders.get(str(order_id))
        if order is None:
            raise NotFoundError(f"order not found: {order_id}")
        return order

    # Methods with domain-language aliases.
    deliver_order = None  # assigned after class? We'll define alias below.

    def confirm_delivery(
        self,
        order_id: str,
        delivered_lines: Any = None,
        at: Any = None,
        *,
        lines: Any = None,
        actual_amounts: Optional[dict[str, int]] = None,
        signed_at: Any = None,
        delivered_at: Any = None,
        actor: Optional[str] = None,
        confirmed_by: Optional[str] = None,
        operator: Optional[str] = None,
        operator_id: Optional[str] = None,
        evidence: Optional[dict[str, Any]] = None,
        verify: Optional[bool] = None,
        refund_policy: str = "pro_rata",
        delivery_id: Optional[str] = None,
        mandate_id: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            order = self._get_order(order_id)
            when = parse_dt(at) if at else now_dt()
            signature_time = signed_at or delivered_at
            event_when = parse_dt(signature_time) if signature_time else when
            acting_as_operator = bool(operator or operator_id or verify is True and (operator or operator_id))
            actor_id = confirmed_by or actor or order["confirmed_by"]

            # A delegate's pending confirmation is checked at action time.
            # Operators may audit an already-signed purchase after revocation.
            if not (operator or operator_id):
                cart = self.carts.get(order["cart"], order)
                self._authorize_actor(actor_id, cart, when, mandate_id)

            selected = lines if lines is not None else delivered_lines
            amounts = self._normalize_delivery_selection(order, selected, actual_amounts)
            delivery_record_id = delivery_id or self._next_id("d")
            if delivery_record_id in self.deliveries:
                raise ConflictError(f"delivery already exists: {delivery_record_id}")

            delivery_lines: list[dict[str, Any]] = []
            total_actual = 0
            total_refunded_self = 0
            all_done = True
            any_delivered = False

            for order_line in order["lines"]:
                line_id = order_line["line"]
                original_amount = order_line["amount"]
                actual = amounts.get(line_id, 0)
                if actual > original_amount:
                    raise ValidationError(
                        "confirmed amount cannot exceed original line amount",
                        details={"line": line_id, "actual": actual, "original": original_amount},
                    )
                if actual > 0:
                    any_delivered = True
                if actual < original_amount:
                    all_done = False

                # Grant validity follows the delivery/signature event time.
                for part in order_line["funding"]:
                    if part["type"] != "grant" or part.get("hold_id") is None:
                        continue
                    grant = self.grants[part["grant"]]
                    if actual > 0 and not within(event_when, grant["from"], grant["until"]):
                        raise CommitmentExpiredError(
                            "funding commitment is outside grant period at delivery; recommit required",
                            details={"line": line_id, "grant": part["grant"], "delivery_at": iso(event_when)},
                        )

                kept_by_part = self._kept_amounts(order_line["funding"], actual, refund_policy)
                out_funding = []
                line_refunded_self = 0
                for part, kept in zip(order_line["funding"], kept_by_part):
                    original_part_amount = part["amount"]
                    canceled = original_part_amount - kept
                    out_part = {
                        "type": part["type"],
                        "kind": part.get("kind"),
                        "grant": part.get("grant"),
                        "original_amount": original_part_amount,
                        "amount": kept,
                        "canceled_amount": canceled,
                        "expires_at": part.get("expires_at"),
                        "until": part.get("until") or part.get("expires_at"),
                        "expiration_date": (part.get("expires_at") or "").split("T", 1)[0] or None,
                        "hold_id": part.get("hold_id"),
                        "charge_id": part.get("charge_id"),
                        "refunds": [],
                        "returns": [],
                    }
                    if part["type"] == "grant":
                        hold_id = part["hold_id"]
                        grant = self.grants[part["grant"]]
                        if canceled > 0:
                            if within(event_when, grant["from"], grant["until"]):
                                self._release_hold(hold_id, canceled, event_when, reason="undelivered_line_or_amount")
                                out_part["returns"].append(
                                    {
                                        "amount": canceled,
                                        "at": iso(event_when),
                                        "status": "released_available",
                                        "reusable": True,
                                        "unusable_reason": None,
                                    }
                                )
                            else:
                                reason = "grant_period_ended" if event_when >= parse_dt(grant["until"]) else "grant_period_not_started"
                                self._expire_hold(hold_id, canceled, event_when, reason=reason)
                                out_part["returns"].append(
                                    {
                                        "amount": canceled,
                                        "at": iso(event_when),
                                        "status": "returned_expired",
                                        "reusable": False,
                                        "unusable_reason": reason,
                                        "explanation": "Returned quota is recorded but cannot be reused or rolled into a new grant period.",
                                    }
                                )
                        if kept > 0:
                            self._use_hold(hold_id, kept, event_when, reason="delivered")
                            charge = self._create_charge(
                                part["grant"],
                                kept,
                                source_type="delivery",
                                source_id=delivery_record_id,
                                line_id=line_id,
                                metadata={"order": order_id, "cart": order["cart"]},
                                effective_at=event_when,
                            )
                            part["charge_id"] = charge["id"]
                            out_part["charge_id"] = charge["id"]
                    else:
                        if canceled > 0:
                            line_refunded_self += canceled
                            refund = {
                                "id": self._next_id("sr"),
                                "amount": canceled,
                                "at": iso(when),
                                "reason": "undelivered_or_unconfirmed_line",
                                "reusable": True,
                                "status": "self_refunded",
                            }
                            out_part["refunds"].append(refund)
                    out_funding.append(out_part)

                total_actual += actual
                total_refunded_self += line_refunded_self
                delivery_lines.append(
                    {
                        "line": line_id,
                        "category": order_line["category"],
                        "original_amount": original_amount,
                        "confirmed_amount": actual,
                        "delivered": actual > 0,
                        "funding": out_funding,
                        "self_refunded_amount": line_refunded_self,
                    }
                )
                order_line["delivered_amount"] = actual
                order_line["confirmed_at"] = iso(when)

            review_status = "verified" if (operator or operator_id or verify is True) else "confirmed_pending_audit"
            delivery = {
                "delivery_id": delivery_record_id,
                "order_id": order_id,
                "cart": order["cart"],
                "household": order["household"],
                "beneficiary": order["beneficiary"],
                "merchant": order.get("merchant"),
                "confirmed_by": actor_id,
                "operator": operator or operator_id,
                "authorization_mode": "operator_audit" if operator or operator_id else "actor",
                "confirmed_at": iso(when),
                "signed_at": iso(event_when) if signature_time else None,
                "event_at": iso(event_when),
                "evidence": evidence or {},
                "actual_amount": total_actual,
                "refunded_amount": 0,
                "self_refunded_amount": total_refunded_self,
                "review_status": review_status,
                "settlement_id": None,
                "lines": delivery_lines,
            }
            self.deliveries[delivery_record_id] = delivery
            order["delivery_ids"].append(delivery_record_id)
            order["self_refunded_amount"] = order.get("self_refunded_amount", 0) + total_refunded_self
            if total_actual == 0:
                order["status"] = "delivery_failed"
            elif all_done:
                order["status"] = "delivered"
            else:
                order["status"] = "partially_delivered"
            self._event("delivery.confirmed", delivery=delivery_record_id, order=order_id, amount=total_actual)
            return self._copy(delivery)

    deliver_order = confirm_delivery

    @staticmethod
    def _normalize_delivery_selection(
        order: dict[str, Any],
        selected: Any,
        actual_amounts: Optional[dict[str, int]],
    ) -> dict[str, int]:
        amount_by_line = {line["line"]: line["amount"] for line in order["lines"]}
        result: dict[str, int] = {}
        if actual_amounts:
            for key, value in actual_amounts.items():
                if key not in amount_by_line:
                    raise NotFoundError(f"order line not found: {key}")
                result[str(key)] = require_int(value, field=f"actual_amounts.{key}", minimum=0)
        if selected is None and not actual_amounts:
            return dict(amount_by_line)
        if isinstance(selected, dict):
            # Allow {"a1": 123} or {"a1": {"amount": 123}}.
            for key, value in selected.items():
                if key not in amount_by_line:
                    raise NotFoundError(f"order line not found: {key}")
                if isinstance(value, dict):
                    if value.get("delivered") is False or value.get("accepted") is False:
                        result[key] = 0
                    else:
                        amount = value.get("confirmed_amount", value.get("actual_amount", value.get("amount")))
                        result[key] = amount_by_line[key] if amount is None else require_int(amount, field=f"line.{key}.amount")
                else:
                    result[key] = require_int(value, field=f"line.{key}.amount")
            return result
        if isinstance(selected, (str, int)):
            selected = [selected]
        if isinstance(selected, list):
            for item in selected:
                if isinstance(item, (str, int)):
                    line_id = str(item)
                    if line_id not in amount_by_line:
                        raise NotFoundError(f"order line not found: {line_id}")
                    result[line_id] = amount_by_line[line_id]
                elif isinstance(item, dict):
                    line_id = str(item.get("line") or item.get("line_id"))
                    if line_id not in amount_by_line:
                        raise NotFoundError(f"order line not found: {line_id}")
                    if item.get("delivered") is False or item.get("accepted") is False:
                        result[line_id] = 0
                    else:
                        amount = item.get("confirmed_amount", item.get("actual_amount", item.get("amount")))
                        result[line_id] = amount_by_line[line_id] if amount is None else require_int(amount, field=f"line.{line_id}.amount")
                else:
                    raise ValidationError("delivered line entries must be line ids or objects")
            return result
        raise ValidationError("delivered_lines must be a list, map, or line id")

    def _kept_amounts(self, parts: list[dict[str, Any]], actual: int, policy: str) -> list[int]:
        originals = [p["amount"] for p in parts]
        if actual == 0:
            return [0] * len(parts)
        if actual >= sum(originals):
            return originals
        if policy == "waterfall":
            kept: list[int] = []
            remaining = actual
            for amount in originals:
                take = min(amount, remaining)
                kept.append(take)
                remaining -= take
            return kept
        # Pro-rata preserves the original sharing ratio.  Largest remainder
        # gives an exact integer-cent allocation with deterministic tie order.
        total = sum(originals)
        floors = [actual * amount // total for amount in originals]
        remainder = actual - sum(floors)
        # Largest fractional remainder gets the first cent; equal fractions keep
        # original line/funding-part order.
        candidates = sorted(range(len(parts)), key=lambda i: ((actual * originals[i]) % total, -i), reverse=True)
        for i in candidates[:remainder]:
            floors[i] += 1
        return floors

    def cancel_order(
        self,
        order_id: str,
        at: Any = None,
        *,
        actor: Optional[str] = None,
        operator: Optional[str] = None,
        reason: str = "canceled_before_delivery",
    ) -> dict[str, Any]:
        """Cancel all undelivered lines and release their funding holds."""
        delivery = self.confirm_delivery(
            order_id,
            [],
            at=at,
            actor=actor,
            operator=operator,
            evidence={"cancel_reason": reason},
        )
        with self.lock:
            order = self._get_order(order_id)
            order["status"] = "canceled"
            order["cancel_reason"] = reason
            order["cancellation_delivery_id"] = delivery["delivery_id"]
            return self._copy(order)

    def verify_delivery(self, delivery_id: str, operator: str, at: Any = None) -> dict[str, Any]:
        with self.lock:
            delivery = self.deliveries.get(str(delivery_id))
            if delivery is None:
                raise NotFoundError(f"delivery not found: {delivery_id}")
            delivery["review_status"] = "verified"
            delivery["verified_by"] = operator
            delivery["verified_at"] = iso(parse_dt(at) if at else now_dt())
            self._event("delivery.verified", delivery=delivery_id, operator=operator)
            return self._copy(delivery)

    def refund_delivery(
        self,
        delivery_id: str,
        at: Any = None,
        *,
        amount: Optional[int] = None,
        line: Optional[str] = None,
        reason: str = "late_void_or_refund",
        operator: Optional[str] = None,
    ) -> dict[str, Any]:
        with self.lock:
            delivery = self.deliveries.get(str(delivery_id))
            if delivery is None:
                raise NotFoundError(f"delivery not found: {delivery_id}")
            when = parse_dt(at) if at else now_dt()
            refundable = delivery["actual_amount"] - delivery.get("refunded_amount", 0)
            requested = refundable if amount is None else require_int(amount, field="amount")
            if requested > refundable:
                raise ValidationError("refund exceeds net delivered amount", details={"refundable": refundable})

            # Collect charge-backed funding parts.  A line-scoped refund uses
            # that line's exact original parts when no partial amount is given.
            targets: list[tuple[dict[str, Any], int]] = []
            if line:
                dline = next((x for x in delivery["lines"] if x["line"] == line), None)
                if dline is None:
                    raise NotFoundError(f"delivery line not found: {line}")
                if amount is None:
                    for fpart in dline["funding"]:
                        if fpart["type"] == "grant" and fpart.get("charge_id"):
                            already = sum(r["amount"] for r in fpart.get("refunds", []))
                            targets.append((fpart, fpart["amount"] - already))
                        elif fpart["type"] == "self_pay":
                            already = sum(r["amount"] for r in fpart.get("refunds", []))
                            targets.append((fpart, fpart["amount"] - already))
                else:
                    self._append_pro_rata_targets(dline["funding"], requested, targets)
            else:
                all_parts = [p for dline in delivery["lines"] for p in dline["funding"]]
                self._append_pro_rata_targets(all_parts, requested, targets)

            total_refund = 0
            refund_records: list[dict[str, Any]] = []
            for fpart, part_amount in targets:
                if part_amount <= 0:
                    continue
                total_refund += part_amount
                if fpart["type"] == "grant":
                    refund = self._refund_charge(
                        fpart["charge_id"],
                        part_amount,
                        when,
                        reason=reason,
                        source_type="delivery_refund",
                        source_id=delivery_id,
                    )
                    fpart.setdefault("refunds", []).append({k: refund[k] for k in ("id", "amount", "status", "unusable_reason", "reusable")})
                    refund_records.append(refund)
                else:
                    refund = {
                        "id": self._next_id("sr"),
                        "amount": part_amount,
                        "at": iso(when),
                        "reason": reason,
                        "reusable": True,
                        "status": "self_refunded",
                    }
                    fpart.setdefault("refunds", []).append(refund)
                    refund_records.append(refund)

            delivery["refunded_amount"] = delivery.get("refunded_amount", 0) + total_refund
            adjustment = None
            if delivery.get("settlement_id"):
                adjustment = self._create_adjustment(
                    merchant=delivery["merchant"],
                    original_settlement_id=delivery["settlement_id"],
                    source_type="delivery",
                    source_id=delivery_id,
                    amount=total_refund,
                    at=when,
                    reason=reason,
                    operator=operator,
                    evidence={"line": line, "refunds": refund_records},
                )
            self._event("delivery.refunded", delivery=delivery_id, amount=total_refund)
            return self._copy(
                {
                    "delivery_id": delivery_id,
                    "refunded_amount": total_refund,
                    "refunds": refund_records,
                    "adjustment": adjustment,
                }
            )

    def _append_pro_rata_targets(
        self,
        funding_parts: list[dict[str, Any]],
        requested: int,
        targets: list[tuple[dict[str, Any], int]],
    ) -> None:
        capacities = []
        for part in funding_parts:
            if part["type"] == "grant":
                if not part.get("charge_id"):
                    capacities.append(0)
                else:
                    charge = self.charges[part["charge_id"]]
                    already = sum(r["amount"] for r in charge["refunds"])
                    capacities.append(charge["amount"] - already)
            else:
                already = sum(r["amount"] for r in part.get("refunds", []))
                capacities.append(part["amount"] - already)
        kept = self._kept_amounts([{"amount": c} for c in capacities], requested, "pro_rata")
        for part, amount in zip(funding_parts, kept):
            if amount > 0:
                targets.append((part, amount))

    # ------------------------------------------------------------------
    # Offline receipts, closures and sequence decisions
    # ------------------------------------------------------------------
    def submit_receipt(self, record: dict[str, Any], *, received_at: Any = None) -> dict[str, Any]:
        with self.lock:
            lease_id = str(record.get("lease") or record.get("lease_id") or "")
            seq = require_int(record.get("seq"), field="seq", minimum=1)
            message = str(record.get("message") or "")
            if not lease_id or not message:
                raise ValidationError("lease, seq and message are required")
            occurred_raw = record.get("occurred_at")
            occurred = parse_dt(occurred_raw, field="occurred_at")
            category = optional_text(record.get("category"))
            amount = require_int(record.get("amount"), field="amount", minimum=1)
            key = (lease_id, seq)
            business = {
                "occurred_at": iso(occurred),
                "category": category,
                "amount": amount,
            }

            existing = self.receipts.get(key)
            if existing is not None:
                existing.setdefault("transport_messages", [existing.get("message")])
                if message not in existing["transport_messages"]:
                    existing["transport_messages"].append(message)
                if self._business_signature(existing) != self._business_signature(business):
                    conflict = self._record_conflict(
                        conflict_type="receipt_content_conflict",
                        key={"lease": lease_id, "seq": seq},
                        existing=self._copy(existing),
                        incoming=self._copy(record),
                    )
                    existing.setdefault("conflicts", []).append(conflict["id"])
                    existing.setdefault("conflicting_versions", []).append(self._copy(record))
                    decision = self.sequence_decisions.get(key)
                    if decision and decision.get("decision") == "voided":
                        decision.setdefault("conflicting_receipts", []).append(
                            {
                                "message": message,
                                "receipt_key": [lease_id, seq],
                                "conflict_id": conflict["id"],
                                "kind": "different_content_for_voided_sequence",
                            }
                        )
                    result = self._copy(existing)
                    result["submission_conflict"] = conflict
                    result["incoming_receipt"] = self._copy(record)
                    return result
                return self._copy(existing)

            if message in self.receipt_messages and self.receipt_messages[message] != key:
                # transport message IDs are tracked separately; reuse for a
                # different sequence is a transport-level conflict.
                self._record_conflict(
                    conflict_type="message_id_reused",
                    key={"message": message},
                    existing=self.receipt_messages[message],
                    incoming={"lease": lease_id, "seq": seq},
                )
            self.receipt_messages[message] = key

            receipt = {
                "lease": lease_id,
                "seq": seq,
                "message": message,
                "transport_messages": [message],
                "occurred_at": iso(occurred),
                "category": category,
                "amount": amount,
                "received_at": iso(parse_dt(received_at) if received_at else now_dt()),
                "status": "received",
                "eligibility": "pending",
                "review_status": "unreviewed",
                "settlement_id": None,
                "funding": [],
                "conflicts": [],
            }
            self.receipts[key] = receipt

            decision = self.sequence_decisions.get(key)
            if decision and decision.get("decision") == "voided":
                receipt["status"] = "conflicts_with_void"
                receipt["eligibility"] = "conflict"
                conflict = self._record_conflict(
                    conflict_type="receipt_contradicts_void_decision",
                    key={"lease": lease_id, "seq": seq},
                    existing_decision=self._copy(decision),
                    incoming=self._copy(record),
                )
                receipt["conflicts"].append(conflict["id"])
                decision.setdefault("conflicting_receipts", []).append(
                    {"message": message, "receipt_key": [lease_id, seq], "conflict_id": conflict["id"]}
                )
                self._event("receipt.conflicts_void", lease=lease_id, seq=seq)
                return self._copy(receipt)

            lease = self.leases.get(lease_id)
            if lease is None:
                receipt["status"] = "waiting_for_lease"
                self._event("receipt.waiting_for_lease", lease=lease_id, seq=seq)
                return self._copy(receipt)

            self._evaluate_and_store_receipt(receipt)
            self._flag_if_beyond_closure(receipt)
            return self._copy(receipt)

    @staticmethod
    def _business_signature(row: dict[str, Any]) -> tuple[Any, ...]:
        return (row.get("occurred_at"), row.get("category"), row.get("amount"))

    def _record_conflict(self, conflict_type: str, **details: Any) -> dict[str, Any]:
        conflict = {"id": self._next_id("cf"), "type": conflict_type, "recorded_at": iso(now_dt()), **details}
        self.conflicts.append(conflict)
        self._event("conflict.recorded", conflict=conflict["id"], type=conflict_type)
        return conflict

    def _evaluate_and_store_receipt(self, receipt: dict[str, Any]) -> None:
        lease = self.leases[receipt["lease"]]
        when = parse_dt(receipt["occurred_at"])
        reasons: list[str] = []

        if not within(when, lease["from"], lease["until"]):
            reasons.append("outside_lease_period")

        mandate = None
        if lease.get("mandate"):
            mandate = self.mandates.get(lease["mandate"])
            if mandate is None:
                reasons.append("mandate_source_missing")
            elif not self._mandate_active(mandate, when):
                if mandate.get("revoked_at") and not when < parse_dt(mandate["revoked_at"]):
                    reasons.append("after_mandate_revocation")
                else:
                    reasons.append("mandate_inactive")
        if lease.get("category") is not None and receipt.get("category") != lease["category"]:
            reasons.append("category_not_allowed")

        # Grant periods are judged at actual in-store occurrence.  An inactive
        # explicit source is simply not usable for this occurrence; the voucher
        # is rejected only when no eligible source can support it.
        active_sources = []
        inactive_allocations = []
        for code in lease.get("allocations", {}):
            grant = self.grants.get(code)
            if grant is None:
                reasons.append("grant_source_missing")
                continue
            source_eligible = (
                (grant.get("beneficiary") is None or grant.get("beneficiary") == lease.get("beneficiary"))
                and (grant.get("category") is None or grant.get("category") == receipt.get("category"))
            )
            if within(when, grant["from"], grant["until"]) and source_eligible:
                active_sources.append(code)
            else:
                inactive_allocations.append(code)
        if not active_sources:
            reasons.append("no_active_eligible_grant_at_occurrence")
        if inactive_allocations:
            receipt["inactive_allocations_at_occurrence"] = inactive_allocations

        total_claimed = sum(
            r["amount"]
            for r in self.receipts.values()
            if r["lease"] == lease["lease"]
            and r.get("status") not in {"voided", "rejected"}
            and not self.sequence_decisions.get((lease["lease"], r["seq"]), {}).get("decision") == "voided"
        )
        if total_claimed > sum(lease["allocations"].values()):
            reasons.append("exceeds_lease_allocation")

        if "after_mandate_revocation" in reasons:
            receipt["status"] = "disputed"
            receipt["eligibility"] = "disputed_authorization"
        elif reasons:
            receipt["status"] = "rejected"
            receipt["eligibility"] = "ineligible"
            receipt["rejection_reasons"] = reasons
        else:
            receipt["status"] = "eligible"
            receipt["eligibility"] = "eligible"
            receipt["rejection_reasons"] = []
        receipt["evaluation_reasons"] = reasons

    def _flag_if_beyond_closure(self, receipt: dict[str, Any]) -> None:
        closure = self._current_closure(receipt["lease"])
        if closure and receipt["seq"] > closure["last_seq"]:
            receipt["status"] = "conflict"
            receipt["eligibility"] = "beyond_closure"
            receipt.setdefault("evaluation_reasons", []).append("seq_beyond_closure_last_seq")
            conflict = self._record_conflict(
                conflict_type="receipt_beyond_closure_last_seq",
                key={"lease": receipt["lease"], "seq": receipt["seq"], "closure": closure["closure_id"]},
                receipt=self._copy(receipt),
                closure_last_seq=closure["last_seq"],
            )
            receipt.setdefault("conflicts", []).append(conflict["id"])

    def submit_closure(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            terminal_id = str(record.get("terminal") or record.get("terminal_id") or "")
            lease_id = str(record.get("lease") or record.get("lease_id") or "")
            last_seq = require_int(record.get("last_seq"), field="last_seq", minimum=0)
            if not terminal_id or not lease_id:
                raise ValidationError("terminal and lease are required")
            terminal = self.terminals.get(terminal_id)
            if terminal is None:
                raise NotFoundError("closure can only be submitted by a registered terminal", details={"terminal": terminal_id})
            lease = self.leases.get(lease_id)
            if lease is None:
                raise NotFoundError(f"lease not found: {lease_id}")
            if lease["terminal"] != terminal_id or terminal["merchant"] != lease["merchant"]:
                raise AuthorizationError("terminal is not registered for this lease")
            occurred_at = record.get("occurred_at")
            closure = {
                "closure_id": record.get("closure") or self._next_id("cl"),
                "terminal": terminal_id,
                "lease": lease_id,
                "last_seq": last_seq,
                "occurred_at": iso(parse_dt(occurred_at) if occurred_at else now_dt()),
                "received_at": iso(now_dt()),
                "status": "received",
                "missing_seqs": [],
            }
            self.closures[lease_id].append(closure)
            closure_when = parse_dt(closure["occurred_at"])
            self._evaluate_closure(lease_id, closure, effective_at=closure_when)
            beyond = sorted(
                r["seq"]
                for r in self.receipts.values()
                if r["lease"] == lease_id and r["seq"] > last_seq
            )
            closure["beyond_seqs"] = beyond
            for seq in beyond:
                receipt = self.receipts[(lease_id, seq)]
                if receipt.get("eligibility") != "beyond_closure":
                    self._flag_if_beyond_closure(receipt)
            self._event("closure.submitted", closure=closure["closure_id"], lease=lease_id)
            return self._copy(closure)

    def _current_closure(self, lease_id: str) -> Optional[dict[str, Any]]:
        rows = self.closures.get(lease_id, [])
        return rows[-1] if rows else None

    def _evaluate_closure(
        self,
        lease_id: str,
        closure: dict[str, Any],
        effective_at: Any = None,
    ) -> dict[str, Any]:
        last_seq = closure["last_seq"]
        missing: list[int] = []
        for seq in range(1, last_seq + 1):
            key = (lease_id, seq)
            has_receipt = key in self.receipts
            decision = self.sequence_decisions.get(key)
            if not has_receipt and not (decision and decision.get("decision") == "voided"):
                missing.append(seq)
        closure["missing_seqs"] = missing
        closure["status"] = "complete" if not missing else "incomplete"
        if not missing:
            self._rebuild_lease_funding(lease_id)
            release_at = parse_dt(effective_at) if effective_at else parse_dt(closure["occurred_at"])
            self._release_lease_residual(lease_id, closure, release_at)
        return closure

    def void_sequence(
        self,
        lease: str,
        seq: int,
        operator: str,
        at: Any = None,
        *,
        reason: str = "missing_sequence_confirmed_void",
        presumed_amount: Optional[int] = None,
    ) -> dict[str, Any]:
        with self.lock:
            seq = require_int(seq, field="seq", minimum=1)
            key = (str(lease), seq)
            when = parse_dt(at) if at else now_dt()
            existing_decision = self.sequence_decisions.get(key)
            if existing_decision:
                if existing_decision.get("decision") == "voided":
                    return self._copy(existing_decision)
                raise ConflictError("sequence already has a non-void decision", details=existing_decision)
            receipt = self.receipts.get(key)
            adjustment = None
            if receipt and receipt.get("review_status") == "verified" and receipt.get("funding"):
                # Delegate to receipt void logic for captured/settled vouchers.
                result = self.void_receipt(lease, seq, operator, when, reason=reason)
                adjustment = result.get("adjustment")
            decision = {
                "key": [lease, seq],
                "lease": lease,
                "seq": seq,
                "decision": "voided",
                "operator": operator,
                "decided_at": iso(when),
                "reason": reason,
                "presumed_amount": presumed_amount,
                "original_receipt_message": receipt.get("message") if receipt else None,
                "conflicting_receipts": [],
                "adjustment": adjustment,
            }
            self.sequence_decisions[key] = decision
            if receipt:
                receipt["status"] = "voided"
                receipt["eligibility"] = "voided"
                receipt["review_status"] = "voided"
                receipt["voided_by"] = operator
                receipt["voided_at"] = iso(when)
                receipt["void_reason"] = reason
            closure = self._current_closure(lease)
            if closure:
                self._evaluate_closure(lease, closure, effective_at=when)
            self._event("sequence.voided", lease=lease, seq=seq, operator=operator)
            return self._copy(decision)

    def void_receipt(
        self,
        lease: str,
        seq: int,
        operator: str,
        at: Any = None,
        *,
        reason: str = "operator_void",
    ) -> dict[str, Any]:
        with self.lock:
            key = (str(lease), int(seq))
            receipt = self.receipts.get(key)
            if receipt is None:
                raise NotFoundError(f"receipt not found: {lease}#{seq}")
            when = parse_dt(at) if at else now_dt()
            adjustment = None
            refund_total = 0
            for fpart in receipt.get("funding", []):
                if fpart.get("type") != "grant" or not fpart.get("charge_id"):
                    continue
                charge = self.charges[fpart["charge_id"]]
                already = sum(r["amount"] for r in charge["refunds"])
                refundable = charge["amount"] - already
                if refundable > 0:
                    refund = self._refund_charge(
                        charge["id"],
                        refundable,
                        when,
                        reason=reason,
                        source_type="receipt_void",
                        source_id=f"{lease}#{seq}",
                    )
                    refund_total += refundable
                    fpart.setdefault("refunds", []).append(
                        {k: refund[k] for k in ("id", "amount", "status", "unusable_reason", "reusable")}
                    )
            was_settled = bool(receipt.get("settlement_id"))
            receipt["status"] = "voided"
            receipt["eligibility"] = "voided"
            receipt["review_status"] = "voided"
            receipt["voided_by"] = operator
            receipt["voided_at"] = iso(when)
            receipt["void_reason"] = reason
            if was_settled and refund_total:
                adjustment = self._create_adjustment(
                    merchant=self.leases[lease]["merchant"],
                    original_settlement_id=receipt["settlement_id"],
                    source_type="receipt",
                    source_id=f"{lease}#{seq}",
                    amount=refund_total,
                    at=when,
                    reason=reason,
                    operator=operator,
                )
            # Preserve an explicit sequence decision as the durable decision.
            if key not in self.sequence_decisions:
                self.sequence_decisions[key] = {
                    "key": [lease, seq],
                    "lease": lease,
                    "seq": seq,
                    "decision": "voided",
                    "operator": operator,
                    "decided_at": iso(when),
                    "reason": reason,
                    "conflicting_receipts": [],
                }
            self._event("receipt.voided", lease=lease, seq=seq, operator=operator)
            return self._copy({"receipt": receipt, "refunded_amount": refund_total, "adjustment": adjustment})

    def _lease_funding_order(self, lease_id: str, at: Any = None) -> list[dict[str, Any]]:
        lease = self.leases[lease_id]
        when = parse_dt(at) if at else None
        grants = []
        for code in lease["allocations"]:
            grant = self.grants.get(code)
            if grant is None:
                continue
            if when is not None and not within(when, grant["from"], grant["until"]):
                continue
            if grant.get("beneficiary") is not None and grant.get("beneficiary") != lease.get("beneficiary"):
                continue
            if grant.get("category") is not None and lease.get("category") is not None and grant.get("category") != lease.get("category"):
                continue
            grants.append(grant)
        resident = [g for g in grants if g.get("beneficiary") is not None]
        shared = [g for g in grants if g.get("beneficiary") is None]
        resident.sort(key=lambda g: (parse_dt(g["until"]), g["grant"]))
        shared.sort(key=lambda g: (parse_dt(g["until"]), g["grant"]))
        return resident + shared

    def _rebuild_lease_funding(self, lease_id: str) -> None:
        """Assign explicit lease source capacity in terminal sequence order."""
        lease = self.leases[lease_id]
        capacity = dict(lease["allocations"])
        ordered = sorted(
            (r for r in self.receipts.values() if r["lease"] == lease_id and r.get("eligibility") != "voided" and r.get("status") != "voided"),
            key=lambda r: r["seq"],
        )
        for receipt in ordered:
            source_order = self._lease_funding_order(lease_id, receipt["occurred_at"])
            if receipt.get("settlement_id") and receipt.get("funding"):
                # Immutable settled funding; subtract it from capacity.
                for part in receipt["funding"]:
                    if part.get("grant"):
                        capacity[part["grant"]] = capacity.get(part["grant"], 0) - part["amount"]
                continue
            remaining = receipt["amount"]
            funding = []
            for grant in source_order:
                code = grant["grant"]
                take = min(remaining, max(0, capacity.get(code, 0)))
                if take <= 0:
                    continue
                capacity[code] -= take
                remaining -= take
                funding.append(
                    {
                        "type": "grant",
                        "grant": code,
                        "amount": take,
                        "expires_at": grant["until"],
                        "until": grant["until"],
                        "expiration_date": grant["until"].split("T", 1)[0],
                        "hold_id": f"hold:lease:{lease_id}:{code}",
                        "charge_id": next(
                            (p.get("charge_id") for p in receipt.get("funding", []) if p.get("grant") == code and p.get("charge_id")),
                            None,
                        ),
                    }
                )
            if remaining > 0:
                receipt["funding_error"] = "lease allocation insufficient"
            else:
                receipt["funding"] = funding
                receipt.pop("funding_error", None)

    def _release_lease_residual(self, lease_id: str, closure: dict[str, Any], when: Any) -> None:
        lease = self.leases[lease_id]
        release_when = parse_dt(when)
        self._rebuild_lease_funding(lease_id)
        used_by_grant: dict[str, int] = defaultdict(int)
        for receipt in self.receipts.values():
            if receipt["lease"] != lease_id or receipt.get("status") == "voided":
                continue
            for part in receipt.get("funding", []):
                if part.get("grant"):
                    used_by_grant[part["grant"]] += part["amount"]
        for code, allocation in lease["allocations"].items():
            hold_id = f"hold:lease:{lease_id}:{code}"
            hold = self.holds.get(hold_id)
            if hold is None:
                continue
            target_released = max(0, allocation - used_by_grant.get(code, 0))
            # Closure accounting can adjust the unreleased residual once.
            self._set_release_target(
                hold_id,
                target_released,
                release_when,
                reason=f"closure {closure['closure_id']} residual accounting",
            )
        closure["residual_released"] = True
        closure["residual_released_at"] = iso(release_when)

    def verify_receipt(
        self,
        lease: str,
        seq: int,
        operator: str,
        at: Any = None,
    ) -> dict[str, Any]:
        with self.lock:
            key = (str(lease), int(seq))
            receipt = self.receipts.get(key)
            if receipt is None:
                raise NotFoundError(f"receipt not found: {lease}#{seq}")
            if receipt["eligibility"] != "eligible":
                raise ConflictError("only eligible receipts can be verified", details={"status": receipt["status"]})
            when = parse_dt(at) if at else now_dt()
            self._rebuild_lease_funding(str(lease))
            if receipt.get("funding_error"):
                raise ConflictError(
                    "receipt cannot be funded from active lease allocations",
                    details={"lease": lease, "seq": seq, "funding_error": receipt["funding_error"]},
                )
            occurrence = parse_dt(receipt["occurred_at"])
            for part in receipt.get("funding", []):
                if part["type"] != "grant" or part.get("charge_id"):
                    continue
                hold_id = part["hold_id"]
                self._use_hold(hold_id, part["amount"], occurrence, reason="offline_receipt_verified")
                charge = self._create_charge(
                    part["grant"],
                    part["amount"],
                    source_type="offline_receipt",
                    source_id=f"{lease}#{seq}",
                    metadata={"lease": lease, "seq": seq},
                    effective_at=occurrence,
                )
                part["charge_id"] = charge["id"]
            receipt["review_status"] = "verified"
            receipt["verified_by"] = operator
            receipt["verified_at"] = iso(when)
            self._event("receipt.verified", lease=lease, seq=seq, operator=operator)
            return self._copy(receipt)

    def verify_lease_receipts(self, lease: str, operator: str, at: Any = None) -> dict[str, Any]:
        with self.lock:
            lease_id = str(lease)
            closure = self._current_closure(lease_id)
            if closure and closure["status"] != "complete":
                raise ConflictError("closure is missing sequences", details={"missing_seqs": closure["missing_seqs"]})
            verified = []
            eligible = sorted(
                (r for r in self.receipts.values() if r["lease"] == lease_id and r["eligibility"] == "eligible"),
                key=lambda r: r["seq"],
            )
            for receipt in eligible:
                if receipt["review_status"] != "verified":
                    verified.append(self.verify_receipt(lease_id, receipt["seq"], operator, at))
            return {"lease": lease_id, "verified": verified}

    # ------------------------------------------------------------------
    # Settlements and adjustments
    # ------------------------------------------------------------------
    def apply_settlement(
        self,
        merchant: str,
        items: Sequence[Any],
        *,
        settlement_id: Optional[str] = None,
        note: Optional[str] = None,
        at: Any = None,
    ) -> dict[str, Any]:
        with self.lock:
            items = list(items or [])
            sid = settlement_id or self._next_id("set")
            if sid in self.settlements:
                raise ConflictError(f"settlement already exists: {sid}")
            when = parse_dt(at) if at else now_dt()
            refs = [self._parse_item_ref(item) for item in items]
            settlement = {
                "settlement_id": sid,
                "merchant": str(merchant),
                "status": "applied",
                "note": note,
                "applied_at": iso(when),
                "issued_at": None,
                "operator": None,
                "items": [{"ref": ref} for ref in refs],
                "voucher_amount": 0,
                "adjustment_amount": 0,
                "total_amount": 0,
                "included_adjustments": [],
            }
            self.settlements[sid] = settlement
            return self._copy(settlement)

    @staticmethod
    def _parse_item_ref(item: Any) -> dict[str, Any]:
        if isinstance(item, str):
            if "#" in item:
                lease, seq = item.rsplit("#", 1)
                return {"kind": "receipt", "lease": lease, "seq": int(seq), "ref": item}
            return {"kind": "delivery", "delivery_id": item, "ref": item}
        if isinstance(item, dict):
            kind = item.get("kind") or item.get("type")
            if kind in {"receipt", "offline_receipt"}:
                lease = item.get("lease")
                seq = int(item.get("seq"))
                return {"kind": "receipt", "lease": lease, "seq": seq, "ref": f"{lease}#{seq}"}
            delivery_id = item.get("delivery_id") or item.get("id")
            if delivery_id:
                return {"kind": "delivery", "delivery_id": str(delivery_id), "ref": str(delivery_id)}
        raise ValidationError(f"unrecognized settlement item: {item!r}")

    def issue_settlement(
        self,
        settlement_id: Optional[str] = None,
        operator: Optional[str] = None,
        at: Any = None,
        *,
        merchant: Optional[str] = None,
        items: Optional[Sequence[Any]] = None,
        note: Optional[str] = None,
        include_pending_adjustments: bool = True,
    ) -> dict[str, Any]:
        with self.lock:
            if settlement_id is None:
                if not merchant:
                    raise ValidationError("provide settlement_id or merchant")
                application = self.apply_settlement(merchant, items or [], note=note, at=at)
                settlement_id = application["settlement_id"]
            settlement = self.settlements[str(settlement_id)]
            if settlement["status"] == "issued":
                return self._copy(settlement)
            if merchant and settlement["merchant"] != merchant:
                raise AuthorizationError("settlement belongs to another merchant")
            when = parse_dt(at) if at else now_dt()
            operator_id = operator or "operator"
            issued_items: list[dict[str, Any]] = []
            voucher_total = 0

            for placeholder in settlement["items"]:
                ref = placeholder["ref"]
                if ref["kind"] == "delivery":
                    item = self._settle_delivery(ref["delivery_id"], settlement["settlement_id"], operator_id, when)
                else:
                    item = self._settle_receipt(ref["lease"], ref["seq"], settlement["settlement_id"], operator_id, when)
                if item["merchant"] != settlement["merchant"]:
                    raise AuthorizationError("item belongs to another merchant", details={"ref": ref})
                voucher_total += item["amount"]
                issued_items.append(item)

            adjustment_total = 0
            included_adjustments = []
            if include_pending_adjustments:
                for adjustment in list(self.adjustments.values()):
                    if adjustment["merchant"] != settlement["merchant"] or adjustment["status"] != "pending":
                        continue
                    adjustment["status"] = "included"
                    adjustment["settlement_id"] = settlement["settlement_id"]
                    adjustment_total += adjustment["amount"]
                    included_adjustments.append(adjustment["adjustment_id"])
                    issued_items.append(
                        {
                            "kind": "adjustment",
                            "ref": adjustment["adjustment_id"],
                            "amount": adjustment["amount"],
                            "adjustment_id": adjustment["adjustment_id"],
                            "original_settlement_id": adjustment["original_settlement_id"],
                            "reason": adjustment["reason"],
                            "basis": adjustment["basis"],
                        }
                    )

            if not issued_items:
                raise ValidationError("settlement has no vouchers or pending adjustments")

            settlement.update(
                {
                    "status": "issued",
                    "operator": operator_id,
                    "issued_at": iso(when),
                    "items": issued_items,
                    "voucher_amount": voucher_total,
                    "adjustment_amount": adjustment_total,
                    "total_amount": voucher_total + adjustment_total,
                    "included_adjustments": included_adjustments,
                }
            )
            self._event("settlement.issued", settlement=settlement["settlement_id"], total=settlement["total_amount"])
            return self._copy(settlement)

    def _settle_delivery(self, delivery_id: str, settlement_id: str, operator: str, when: Any) -> dict[str, Any]:
        delivery = self.deliveries.get(delivery_id)
        if delivery is None:
            raise NotFoundError(f"delivery not found: {delivery_id}")
        if delivery.get("settlement_id"):
            raise ConflictError("delivery is already settled", details={"delivery": delivery_id})
        if delivery["review_status"] != "verified":
            # The operator issuing the settlement performs the final check.
            delivery["review_status"] = "verified"
            delivery["verified_by"] = operator
            delivery["verified_at"] = iso(when)
        net_amount = delivery["actual_amount"] - delivery.get("refunded_amount", 0)
        if net_amount < 0:
            raise ConflictError("delivery refund exceeds delivered amount")
        quota_amount = 0
        self_pay_amount = 0
        for line in delivery["lines"]:
            for part in line["funding"]:
                refunded = sum(r.get("amount", 0) for r in part.get("refunds", []))
                net_part = max(0, part.get("amount", 0) - refunded)
                if part["type"] == "grant":
                    quota_amount += net_part
                else:
                    self_pay_amount += net_part
        delivery["settlement_id"] = settlement_id
        return {
            "kind": "delivery",
            "ref": delivery_id,
            "delivery_id": delivery_id,
            "order_id": delivery["order_id"],
            "merchant": delivery["merchant"],
            "amount": net_amount,
            "quota_amount": quota_amount,
            "self_pay_amount": self_pay_amount,
            "gross_delivered_amount": delivery["actual_amount"],
            "already_refunded_amount": delivery.get("refunded_amount", 0),
            "basis": "delivered and confirmed line amounts; amount includes collected self-pay when applicable",
            "lines": self._copy(delivery["lines"]),
            "signed_at": delivery.get("signed_at"),
            "event_at": delivery.get("event_at"),
        }

    def _settle_receipt(self, lease_id: str, seq: int, settlement_id: str, operator: str, when: Any) -> dict[str, Any]:
        key = (lease_id, seq)
        receipt = self.receipts.get(key)
        if receipt is None:
            raise NotFoundError(f"receipt not found: {lease_id}#{seq}")
        if receipt.get("settlement_id"):
            raise ConflictError("receipt is already settled", details={"ref": f"{lease_id}#{seq}"})
        closure = self._current_closure(lease_id)
        if closure is None:
            raise ConflictError("lease has no closure voucher", details={"lease": lease_id})
        if closure["status"] != "complete":
            raise ConflictError(
                "closure has unaccounted sequences",
                details={"lease": lease_id, "missing_seqs": closure["missing_seqs"]},
            )
        if receipt["seq"] > closure["last_seq"]:
            raise ConflictError(
                "receipt sequence is beyond the closed last_seq and must be adjudicated",
                details={"lease": lease_id, "seq": seq, "last_seq": closure["last_seq"]},
            )
        if receipt["eligibility"] != "eligible":
            raise ConflictError(
                "receipt is not eligible for normal settlement",
                details={"lease": lease_id, "seq": seq, "eligibility": receipt["eligibility"]},
            )
        if receipt["review_status"] != "verified":
            self.verify_receipt(lease_id, seq, operator, when)
        lease = self.leases[lease_id]
        funded_amount = sum(part.get("amount", 0) for part in receipt.get("funding", []) if part.get("type") == "grant")
        self_pay_amount = receipt["amount"] - funded_amount
        receipt["settlement_id"] = settlement_id
        return {
            "kind": "receipt",
            "ref": f"{lease_id}#{seq}",
            "lease": lease_id,
            "seq": seq,
            "message": receipt["message"],
            "merchant": lease["merchant"],
            "terminal": lease["terminal"],
            "amount": receipt["amount"],
            "quota_amount": funded_amount,
            "self_pay_amount": self_pay_amount,
            "occurred_at": receipt["occurred_at"],
            "category": receipt["category"],
            "basis": "actual in-store occurrence; mandate/category/grant period checked",
            "funding": self._copy(receipt["funding"]),
        }

    def _create_adjustment(
        self,
        *,
        merchant: str,
        original_settlement_id: str,
        source_type: str,
        source_id: str,
        amount: int,
        at: Any,
        reason: str,
        operator: Optional[str],
        evidence: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        original = self.settlements.get(original_settlement_id)
        adjustment = {
            "adjustment_id": self._next_id("adj"),
            "merchant": merchant,
            "status": "pending",
            "amount": -abs(amount),
            "positive_amount": abs(amount),
            "original_settlement_id": original_settlement_id,
            "original_settlement_total_at_issue": original["total_amount"] if original else None,
            "source_type": source_type,
            "source_id": source_id,
            "reason": reason,
            "operator": operator,
            "created_at": iso(parse_dt(at) if at else now_dt()),
            "settlement_id": None,
            "basis": "late void/refund; original settlement remains unchanged and is interpreted by its issued vouchers",
            "evidence": evidence or {},
        }
        self.adjustments[adjustment["adjustment_id"]] = adjustment
        self._event("adjustment.created", adjustment=adjustment["adjustment_id"], source=source_id)
        return self._copy(adjustment)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def resident_view(self, beneficiary: str, household: Optional[str] = None, at: Any = None) -> dict[str, Any]:
        with self.lock:
            when = parse_dt(at) if at else now_dt()
            self.expire_due_commitments(when)
            household = household or self._infer_household(beneficiary)
            grants = []
            for grant in self.grants.values():
                if grant["household"] != household:
                    continue
                if grant["beneficiary"] is not None and grant["beneficiary"] != beneficiary:
                    continue
                summary = self.grant_summary(grant["grant"], when)
                grants.append(summary)
            usable = [g for g in grants if g["usable_now"] and g["available_amount"] > 0]

            pending_orders = []
            for order in self.orders.values():
                if order["beneficiary"] == beneficiary and order["status"] in {"awaiting_delivery", "partially_delivered"}:
                    pending_orders.append(self._copy({k: order[k] for k in ("order_id", "status", "total_amount", "self_pay_amount", "confirmed_at")}))

            waiting_upload = []
            disputes = []
            for lease in self.leases.values():
                if lease["beneficiary"] != beneficiary:
                    continue
                closure = self._current_closure(lease["lease"])
                lease_receipts = [r for r in self.receipts.values() if r["lease"] == lease["lease"]]
                if closure and closure["status"] == "incomplete":
                    waiting_upload.append(
                        {
                            "lease": lease["lease"],
                            "terminal": lease["terminal"],
                            "state": "missing_sequences",
                            "last_seq": closure["last_seq"],
                            "missing_seqs": closure["missing_seqs"],
                        }
                    )
                elif not closure:
                    waiting_upload.append(
                        {
                            "lease": lease["lease"],
                            "terminal": lease["terminal"],
                            "state": "awaiting_closure",
                            "known_seqs": sorted(r["seq"] for r in lease_receipts),
                            "lease_until": lease["until"],
                        }
                    )
                for receipt in lease_receipts:
                    if (
                    receipt.get("eligibility") in {"disputed_authorization", "conflict", "beyond_closure"}
                    or receipt.get("status") in {"disputed", "conflict", "conflicts_with_void"}
                ):
                        disputes.append(
                            {
                                "lease": receipt["lease"],
                                "seq": receipt["seq"],
                                "message": receipt["message"],
                                "amount": receipt["amount"],
                                "occurred_at": receipt["occurred_at"],
                                "status": receipt["status"],
                                "eligibility": receipt["eligibility"],
                                "conflicts": receipt.get("conflicts", []),
                            }
                        )
            return {
                "beneficiary": beneficiary,
                "household": household,
                "at": iso(when),
                "usable_grants": self._copy(usable),
                "usable_balances": self._copy(usable),
                "grants": self._copy(grants),
                "pending_orders": self._copy(pending_orders),
                "waiting_upload": self._copy(waiting_upload),
                "waiting_for_vouchers": self._copy(waiting_upload),
                "disputes": self._copy(disputes),
                "pending_disputes": self._copy(disputes),
            }

    def _infer_household(self, beneficiary: str) -> str:
        for grant in self.grants.values():
            if grant.get("beneficiary") == beneficiary:
                return grant["household"]
        for cart in self.carts.values():
            if cart["beneficiary"] == beneficiary:
                return cart["household"]
        for lease in self.leases.values():
            if lease["beneficiary"] == beneficiary:
                # Lease does not carry household; allocations do.
                for code in lease["allocations"]:
                    if code in self.grants:
                        return self.grants[code]["household"]
        raise NotFoundError(f"cannot infer household for beneficiary: {beneficiary}; pass household explicitly")

    def merchant_view(self, merchant: str, at: Any = None) -> dict[str, Any]:
        with self.lock:
            when = parse_dt(at) if at else now_dt()
            lease_ids = {lid for lid, lease in self.leases.items() if lease["merchant"] == merchant}
            receipts = [r for r in self.receipts.values() if r["lease"] in lease_ids]
            online_orders = [o for o in self.orders.values() if o.get("merchant") == merchant]
            deliveries = [d for d in self.deliveries.values() if d.get("merchant") == merchant]
            settlements = [s for s in self.settlements.values() if s["merchant"] == merchant]
            adjustments = [a for a in self.adjustments.values() if a["merchant"] == merchant]

            verified_unsettled_delivery = 0
            pending_delivery_review = 0
            reimbursable_delivery = 0
            for delivery in deliveries:
                if delivery.get("settlement_id"):
                    continue
                net = delivery["actual_amount"] - delivery.get("refunded_amount", 0)
                if net <= 0:
                    continue
                # A resident/delegate-confirmed delivery is eligible for a
                # merchant settlement application; operator verification is
                # completed when the settlement is issued if not earlier.
                reimbursable_delivery += net
                if delivery["review_status"] == "verified":
                    verified_unsettled_delivery += net
                else:
                    pending_delivery_review += net

            verified_unsettled_receipt = 0
            pending_receipt_review = 0
            reimbursable_receipt = 0
            disputed_amount = 0
            missing_vouchers = []
            for receipt in receipts:
                if receipt.get("settlement_id"):
                    continue
                if receipt.get("eligibility") == "eligible":
                    reimbursable_receipt += receipt["amount"]
                    if receipt.get("review_status") == "verified":
                        verified_unsettled_receipt += receipt["amount"]
                    else:
                        pending_receipt_review += receipt["amount"]
                elif (
                    receipt.get("status") in {"disputed", "conflict", "conflicts_with_void"}
                    or "disputed" in receipt.get("eligibility", "")
                    or "conflict" in receipt.get("eligibility", "")
                    or receipt.get("eligibility") == "beyond_closure"
                ):
                    disputed_amount += receipt["amount"]

            for lease_id in sorted(lease_ids):
                closure = self._current_closure(lease_id)
                if closure and closure["status"] == "incomplete":
                    missing_vouchers.append(
                        {"lease": lease_id, "closure": closure["closure_id"], "missing_seqs": closure["missing_seqs"]}
                    )
                elif not closure:
                    missing_vouchers.append({"lease": lease_id, "missing": "closure"})

            settled_total = sum(s["total_amount"] for s in settlements if s["status"] == "issued")
            return {
                "merchant": merchant,
                "at": iso(when),
                "reimbursable_amount": reimbursable_delivery + reimbursable_receipt,
                "reimbursable_now": reimbursable_delivery + reimbursable_receipt,
                "eligible_unsettled_delivery_amount": reimbursable_delivery,
                "eligible_unsettled_receipt_amount": reimbursable_receipt,
                "verified_unsettled_delivery_amount": verified_unsettled_delivery,
                "verified_unsettled_receipt_amount": verified_unsettled_receipt,
                "pending_review_amount": pending_delivery_review + pending_receipt_review,
                "disputed_amount": disputed_amount,
                "settled_total_amount": settled_total,
                "pending_adjustment_amount": sum(a["amount"] for a in adjustments if a["status"] == "pending"),
                "missing_vouchers": self._copy(missing_vouchers),
                "missing_receipts": self._copy(missing_vouchers),
                "missing_documents": self._copy(missing_vouchers),
                "receipts": self._copy([self._merchant_safe_receipt(r) for r in receipts]),
                "deliveries": self._copy([self._merchant_safe_delivery(d) for d in deliveries]),
                "orders": self._copy([self._merchant_safe_order(o) for o in online_orders]),
                "settlements": self._copy(settlements),
                "adjustments": self._copy(adjustments),
            }

    @staticmethod
    def _merchant_safe_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
        return {
            k: receipt.get(k)
            for k in (
                "lease",
                "seq",
                "message",
                "amount",
                "category",
                "occurred_at",
                "status",
                "eligibility",
                "review_status",
                "settlement_id",
                "conflicts",
                "rejection_reasons",
            )
        }

    @staticmethod
    def _merchant_safe_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
        return {
            k: delivery.get(k)
            for k in (
                "delivery_id",
                "order_id",
                "actual_amount",
                "refunded_amount",
                "review_status",
                "settlement_id",
                "lines",
                "signed_at",
                "event_at",
            )
        }

    @staticmethod
    def _merchant_safe_order(order: dict[str, Any]) -> dict[str, Any]:
        # Merchant sees only its own order.  Household internal identities and
        # unrelated purchases are absent by query filtering.
        return {
            k: order.get(k)
            for k in ("order_id", "status", "total_amount", "lines", "confirmed_at", "delivery_ids")
        }

    def admin_view(self) -> dict[str, Any]:
        with self.lock:
            return {
                "grants": [self.grant_summary(code) for code in sorted(self.grants)],
                "mandates": self._copy(list(self.mandates.values())),
                "leases": self._copy(list(self.leases.values())),
                "receipts": self._copy(list(self.receipts.values())),
                "closures": self._copy([c for rows in self.closures.values() for c in rows]),
                "sequence_decisions": self._copy(list(self.sequence_decisions.values())),
                "conflicts": self._copy(self.conflicts),
                "pending_reviews": {
                    "deliveries": [d["delivery_id"] for d in self.deliveries.values() if d["review_status"] != "verified"],
                    "receipts": [
                        f"{r['lease']}#{r['seq']}"
                        for r in self.receipts.values()
                        if r["eligibility"] == "eligible" and r["review_status"] != "verified"
                    ],
                },
                "settlements": self._copy(list(self.settlements.values())),
                "adjustments": self._copy(list(self.adjustments.values())),
            }

    # Common domain-language aliases for integrations and exploratory use.
    quote = quote_cart
    quote_basket = quote_cart
    calculate_quote = quote_cart
    create_order = confirm_cart
    place_order = confirm_cart
    confirm_order = confirm_cart
    record_delivery = confirm_delivery
    deliver_order = confirm_delivery
    add_receipt = submit_receipt
    record_receipt = submit_receipt
    upload_receipt = submit_receipt
    close_terminal = submit_closure
    submit_closing = submit_closure
    approve_settlement = issue_settlement
    create_settlement = issue_settlement
    get_resident_view = resident_view
    get_merchant_view = merchant_view


# Descriptive aliases for integrations that use a different class vocabulary.
Service = QuotaService
Engine = QuotaService
QuotaEngine = QuotaService
LedgerService = QuotaService
CommunityQuotaService = QuotaService
