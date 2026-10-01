"""
Compatibility helpers for pretix's installment plans.

Installments live in the ``pretix.efcc`` app, which only exists on the EFCC
pretix fork. The plugin is also published for upstream pretix, so nothing here
may be imported at module scope: every helper resolves the models lazily and
reports "no installments" when the app is absent, which keeps the provider
usable on an installation that has no installment support at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pretix.base.models import Order, OrderPayment

logger = logging.getLogger(__name__)


def installments_available() -> bool:
    """Whether this pretix installation knows about installment plans."""
    return _models() is not None


def _models() -> tuple[Any, Any] | None:
    """
    Return ``(InstallmentPlan, ScheduledInstallment)``, or ``None`` upstream.
    """
    try:
        from pretix.efcc.models import InstallmentPlan, ScheduledInstallment
    except ImportError:
        return None
    return InstallmentPlan, ScheduledInstallment


def scheduled_installment_for_payment(payment: OrderPayment) -> Any | None:
    """
    Return the scheduled installment an ``OrderPayment`` settles, if any.

    pretix links the two the other way round — an installment points at the
    payment that pays it — so a payment that is part of a plan is recognised
    by the installment referencing it, not by a field on the payment.
    """
    models = _models()
    if models is None or not payment.pk:
        return None
    _plan_model, scheduled_installment = models
    return scheduled_installment.objects.filter(payment=payment).first()


def plan_accepts_token(plan: Any) -> bool:
    """
    Whether storing a token on this plan still means anything.

    pretix revokes the token at the provider and clears it when a plan
    completes or is cancelled, so writing one back would record a token that
    no longer exists upstream. A failed plan keeps its token on purpose: the
    customer can recover such a plan by paying again.

    An unrecognised status counts as acceptable, so a plan object without the
    field is not silently refused.
    """
    models = _models()
    if models is None:
        return False
    installment_plan, _scheduled_installment = models
    return getattr(plan, "status", None) not in (
        installment_plan.STATUS_COMPLETED,
        installment_plan.STATUS_CANCELLED,
    )


def plan_for_order(order: Order) -> Any | None:
    """
    Return the installment plan of an order, or ``None`` if it has none.

    Queried through the manager rather than the ``order.installment_plan``
    reverse accessor: the relation only exists once the efcc app is
    installed, so nothing that has to keep working without it can name it.
    """
    models = _models()
    if models is None or not order.pk:
        return None
    installment_plan, _scheduled_installment = models
    return installment_plan.objects.filter(order=order).first()
