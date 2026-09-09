"""Credit ledger.

Balance is a column for read speed and a ledger for truth; they are written in
the same transaction under a row lock, so a double-tap on "make it" cannot spend
the same credit twice. Every charge carries an idempotency key -- the creative
id -- because job retries are normal, not exceptional.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Account, CreditLedger
from app.logging import get_logger

log = get_logger(__name__)

# What each action costs the customer, in credits.
COST = {
    "generate_creative": 1,   # per image call -- a 5-slide carousel costs 5
    "revise_copy": 0,         # re-composite only: no image call, so free
    "regenerate_image": 1,
    "publish_instagram": 0,
    "reference_asset": 0,     # the owner's own photo: no generation to pay for
}


class InsufficientCredits(Exception):
    def __init__(self, balance: int, needed: int) -> None:
        self.balance, self.needed = balance, needed
        super().__init__(f"balance {balance}, needed {needed}")


@dataclass(slots=True)
class ChargeResult:
    charged: int
    balance_after: int
    deduped: bool = False


def cost_of(action: str) -> int:
    return COST.get(action, 1)


def charge(
    db: Session,
    *,
    account_id: uuid.UUID,
    action: str,
    idempotency_key: str,
    units: int = 1,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> ChargeResult:
    """`units` is the number of billable image calls -- one per carousel slide."""
    amount = cost_of(action) * max(units, 0)
    acct = db.execute(
        select(Account).where(Account.id == account_id).with_for_update()
    ).scalar_one()

    if amount == 0:
        return ChargeResult(0, acct.credits_balance)

    existing = db.execute(
        select(CreditLedger).where(CreditLedger.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return ChargeResult(-existing.delta, existing.balance_after, deduped=True)

    if acct.credits_balance < amount:
        raise InsufficientCredits(acct.credits_balance, amount)

    acct.credits_balance -= amount
    entry = CreditLedger(
        account_id=account_id,
        delta=-amount,
        balance_after=acct.credits_balance,
        reason=action,
        ref_type=ref_type,
        ref_id=ref_id,
        idempotency_key=idempotency_key,
    )
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise
    log.info(
        "credits_charged",
        account_id=str(account_id),
        action=action,
        balance=acct.credits_balance,
    )
    return ChargeResult(amount, acct.credits_balance)


def refund(
    db: Session, *, account_id: uuid.UUID, amount: int, reason: str, idempotency_key: str
) -> int:
    """Called when a creative fails after the charge. Silence here is a support ticket."""
    acct = db.execute(
        select(Account).where(Account.id == account_id).with_for_update()
    ).scalar_one()
    if db.execute(
        select(CreditLedger).where(CreditLedger.idempotency_key == idempotency_key)
    ).scalar_one_or_none():
        return acct.credits_balance
    acct.credits_balance += amount
    db.add(
        CreditLedger(
            account_id=account_id,
            delta=amount,
            balance_after=acct.credits_balance,
            reason=reason,
            idempotency_key=idempotency_key,
        )
    )
    db.flush()
    log.info("credits_refunded", account_id=str(account_id), amount=amount)
    return acct.credits_balance


def topup(
    db: Session, *, account_id: uuid.UUID, credits: int, reason: str, idempotency_key: str
) -> int:
    return refund(db, account_id=account_id, amount=credits, reason=reason,
                  idempotency_key=idempotency_key)
