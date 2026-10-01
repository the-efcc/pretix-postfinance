"""
End-to-end tests of installment plans against pretix's own models.

Installments live in the ``pretix.efcc`` app, which only exists on the EFCC
pretix fork, so the whole module is skipped on an installation without it.
Everything here drives pretix's real flow — plan creation at order time, the
first payment through the provider, and the scheduled charges pretix's
periodic task makes afterwards — rather than calling the provider directly.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django_scopes import scopes_disabled
from postfinancecheckout.models import ChargeState, TransactionState
from pretix.base.models import Order, OrderPayment, OrderRefund

pytest.importorskip("pretix.efcc.models", reason="requires pretix with installments")

from pretix.base.services.installments import (
    cancel_installment_plan,
    create_installment_plan,
    process_single_installment,
)
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import METHOD_CONFIG_KEY, PostFinancePaymentProvider
from pretix_postfinance.views import _process_transaction_webhook

TOKEN_ID = 999888
# The connector that processes the interactive payment, and the method
# configuration above it that the later token charges must be restricted to.
CONNECTOR_ID = 116916
METHOD_CONFIG_ID = 80778


@pytest.fixture
def installments_event(event):
    event.currency = "CHF"
    event.save()
    event.settings.set("installments_enabled", True)
    event.settings.set("installments_count", 3)
    event.settings.set("installments_limit_by_event_date", False)
    return event


@pytest.fixture
def alt_currency_event(installments_event):
    installments_event.settings.set("payment_postfinance_alt_currency", "EUR")
    installments_event.settings.set("payment_postfinance_alt_currency_rate", "1.07")
    return installments_event


@pytest.fixture
def installments_order(installments_event, order):
    order.total = Decimal("300.00")
    order.save()
    return order


def fulfilled_transaction(with_token=True):
    token = (
        SimpleNamespace(
            id=TOKEN_ID,
            customer_id="cus_test123",
            customer_email_address="customer@example.org",
        )
        if with_token
        else None
    )
    return SimpleNamespace(
        id=123456,
        state=TransactionState.FULFILL,
        payment_connector_configuration=SimpleNamespace(name="Visa", id=CONNECTOR_ID),
        created_on="2026-01-13T10:00:00Z",
        token=token,
    )


@pytest.fixture
def postfinance(monkeypatch):
    """
    Stand in for the PostFinance API and record every transaction created.

    One stub for the whole flow, so the interactive first payment and the
    later token charges cannot shadow each other's patches.
    """
    api = SimpleNamespace(
        transactions=[], revoked=[], refunds=[], decline=None,
        # When set, the charge call dies after the money has moved -- the
        # transaction exists and is settled, but we never hear the answer.
        lose_response=False,
        transaction=fulfilled_transaction(),
    )

    def create_transaction(self, **kwargs):
        api.transactions.append(kwargs)
        return SimpleNamespace(id=123456 + len(api.transactions))

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        create_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: api.transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient."
        "get_method_configuration_of_connector",
        lambda self, connector_id: (
            METHOD_CONFIG_ID if connector_id == CONNECTOR_ID else None
        ),
    )
    def process_with_token(self, tid):
        if api.lose_response:
            raise PostFinanceError("gateway timeout", status_code=504)
        if api.decline is not None:
            return SimpleNamespace(
                state=ChargeState.FAILED,
                failure_reason=SimpleNamespace(description=api.decline),
                transaction=SimpleNamespace(
                    id=tid,
                    state=TransactionState.FAILED,
                    payment_connector_configuration=None,
                    created_on=None,
                ),
            )
        return SimpleNamespace(
            state=ChargeState.SUCCESSFUL,
            failure_reason=None,
            transaction=SimpleNamespace(
                id=tid,
                state=TransactionState.FULFILL,
                payment_connector_configuration=SimpleNamespace(name="TWINT"),
                created_on="2026-01-13T10:00:00Z",
            ),
        )

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        process_with_token,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.delete_token",
        lambda self, tid: api.revoked.append(tid),
    )

    def refund_transaction(self, **kwargs):
        api.refunds.append(kwargs)
        return SimpleNamespace(
            id=555000 + len(api.refunds),
            state=SimpleNamespace(value="SUCCESSFUL"),
            amount=float(kwargs.get("amount") or 0),
            created_on="2026-01-13T12:00:00Z",
        )

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        refund_transaction,
    )

    # Only the charges pretix schedules carry a token; the first payment is
    # the customer paying on the payment page.
    api.token_charges = lambda: [t for t in api.transactions if t.get("token")]
    return api


@pytest.fixture
def pay_first_installment(postfinance, rf):
    """
    Drive a plan's first payment through the provider, as checkout does.
    """

    def _pay(event, payment, *, pay_alt=False):
        prov = PostFinancePaymentProvider(event)
        req = rf.post("/", {"payment": "postfinance"})
        req.session = {}
        if pay_alt:
            req.session[prov._session_pay_alt_key] = True

        # The first call creates the transaction and sends the customer to
        # PostFinance; returning from the payment page calls it again, and
        # that second call is what settles the payment.
        redirect_url = prov.execute_payment(req, payment)
        assert redirect_url.startswith("https://checkout.postfinance.ch/pay/")
        prov.execute_payment(req, payment)
        payment.refresh_from_db()
        return prov

    return _pay


def first_payment_of(plan):
    return plan.installments.get(installment_number=1).payment


def due(plan, number):
    """
    Load a scheduled installment the way pretix's periodic task does.

    Fetching it through ``plan.installments`` would hand it the test's own
    in-memory plan, whose token predates the first payment.
    """
    return ScheduledInstallment.objects.select_related(
        "plan", "plan__order", "plan__order__event"
    ).get(plan=plan, installment_number=number)


@pytest.mark.django_db
def test_plan_creation_splits_the_order(installments_event, installments_order):
    plan = create_installment_plan(installments_order, "postfinance", 3)

    assert plan.total_installments == 3
    assert [i.amount for i in plan.installments.all()] == [
        Decimal("100.00"),
        Decimal("100.00"),
        Decimal("100.00"),
    ]
    assert first_payment_of(plan).amount == Decimal("100.00")


@pytest.mark.django_db
def test_full_plan_lifecycle_in_event_currency(
    installments_event,
    installments_order,
    pay_first_installment,
    postfinance,
):
    """
    The customer pays the first installment, pretix charges the rest against
    the token, and the plan closes with the token revoked.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)

    pay_first_installment(installments_event, payment)

    # The first payment settles installment one and leaves the token behind.
    plan.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert plan.payment_token["token_id"] == TOKEN_ID
    # The connector that took the interactive payment is what the automatic
    # charges have to be aimed at, so it is recorded with the token.
    assert plan.payment_token[METHOD_CONFIG_KEY] == METHOD_CONFIG_ID
    assert plan.installments_paid == 1
    assert plan.installments.get(installment_number=1).state == (
        ScheduledInstallment.STATE_PAID
    )
    assert plan.status == InstallmentPlan.STATUS_ACTIVE

    for number in (2, 3):
        installment = due(plan, number)
        assert process_single_installment(installment) is True
        installment.refresh_from_db()
        assert installment.state == ScheduledInstallment.STATE_PAID
        assert installment.payment.amount == Decimal("100.00")

    assert [c["currency"] for c in postfinance.token_charges()] == ["CHF", "CHF"]
    assert [c["line_items"][0].amount_including_tax for c in postfinance.token_charges()] == [
        100.00,
        100.00,
    ]
    assert all(c["token"] == TOKEN_ID for c in postfinance.token_charges())
    assert all(
        c["allowed_payment_method_configurations"] == [METHOD_CONFIG_ID]
        for c in postfinance.token_charges()
    )

    plan.refresh_from_db()
    assert plan.status == InstallmentPlan.STATUS_COMPLETED
    assert plan.installments_paid == 3
    assert postfinance.revoked == [TOKEN_ID]

    installments_order.refresh_from_db()
    assert installments_order.pending_sum == Decimal("0.00")
    assert installments_order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_full_plan_lifecycle_in_alternative_currency(
    alt_currency_event,
    installments_order,
    pay_first_installment,
    postfinance,
):
    """
    A plan bought in the alternative currency stays in that currency: every
    scheduled charge converts its own share at the rate the customer was
    quoted, while pretix keeps accounting the order in CHF.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)

    pay_first_installment(alt_currency_event, payment, pay_alt=True)

    payment.refresh_from_db()
    assert payment.info_data["charged_currency"] == "EUR"
    assert payment.info_data["charged_amount"] == "107.00"
    assert payment.amount == Decimal("100.00")

    plan.refresh_from_db()
    assert plan.payment_token["charged_currency"] == "EUR"
    assert plan.payment_token["fx_rate"] == "1.07"
    # The first payment's own converted amount must not be mistaken for a
    # per-installment amount.
    assert "charged_amount" not in plan.payment_token

    for number in (2, 3):
        assert process_single_installment(due(plan, number)) is True

    assert [c["currency"] for c in postfinance.token_charges()] == ["EUR", "EUR"]
    assert [c["line_items"][0].amount_including_tax for c in postfinance.token_charges()] == [
        107.00,
        107.00,
    ]

    # pretix's own records stay in the event currency.
    plan.refresh_from_db()
    assert plan.status == InstallmentPlan.STATUS_COMPLETED
    assert [i.payment.amount for i in plan.installments.order_by("installment_number")] == [
        Decimal("100.00"),
        Decimal("100.00"),
        Decimal("100.00"),
    ]
    installments_order.refresh_from_db()
    assert installments_order.pending_sum == Decimal("0.00")
    assert installments_order.status == Order.STATUS_PAID
    assert postfinance.revoked == [TOKEN_ID]


@pytest.mark.django_db
def test_alternative_currency_rate_change_does_not_reprice_open_installments(
    alt_currency_event,
    installments_order,
    pay_first_installment,
    postfinance,
):
    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(alt_currency_event, first_payment_of(plan), pay_alt=True)

    alt_currency_event.settings.set("payment_postfinance_alt_currency_rate", "2.00")

    assert process_single_installment(due(plan, 2)) is True

    assert postfinance.token_charges()[0]["line_items"][0].amount_including_tax == 107.00


@pytest.mark.django_db
def test_uneven_split_converts_each_share(
    alt_currency_event,
    installments_order,
    pay_first_installment,
    postfinance,
):
    """
    pretix puts the rounding remainder on the last installment; each share is
    converted on its own, so the charges follow that split.
    """
    installments_order.total = Decimal("100.00")
    installments_order.save()

    plan = create_installment_plan(installments_order, "postfinance", 3)
    assert [i.amount for i in plan.installments.all()] == [
        Decimal("33.33"),
        Decimal("33.33"),
        Decimal("33.34"),
    ]

    pay_first_installment(alt_currency_event, first_payment_of(plan), pay_alt=True)
    for number in (2, 3):
        assert process_single_installment(due(plan, number)) is True

    # 33.33 * 1.07 = 35.6631 -> 35.66; 33.34 * 1.07 = 35.6738 -> 35.67
    assert [c["line_items"][0].amount_including_tax for c in postfinance.token_charges()] == [
        35.66,
        35.67,
    ]


@pytest.mark.django_db
def test_an_automatic_charge_can_be_refunded(
    installments_event, installments_order, pay_first_installment, postfinance
):
    """
    An automatic charge used to leave no transaction reference behind, so
    pretix declined to refund it and the money had to be returned from the
    PostFinance dashboard by hand.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(installments_event, first_payment_of(plan))
    assert process_single_installment(due(plan, 2)) is True

    payment = due(plan, 2).payment
    prov = PostFinancePaymentProvider(installments_event)
    assert prov.payment_refund_supported(payment) is True

    refund = installments_order.refunds.create(
        payment=payment,
        provider="postfinance",
        amount=payment.amount,
        source=OrderRefund.REFUND_SOURCE_ADMIN,
        state=OrderRefund.REFUND_STATE_CREATED,
    )
    prov.execute_refund(refund)

    assert postfinance.refunds[0]["transaction_id"] == payment.info_data["transaction_id"]
    assert postfinance.refunds[0]["amount"] == Decimal("100.00")


@pytest.mark.django_db
def test_an_automatic_charge_is_refunded_in_the_currency_it_was_charged_in(
    alt_currency_event, installments_order, pay_first_installment, postfinance
):
    """
    Refunding 100 CHF against a transaction charged as 107 EUR would be
    rejected, or return the wrong amount. The rate recorded on the payment
    is what converts it back.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(alt_currency_event, first_payment_of(plan), pay_alt=True)
    assert process_single_installment(due(plan, 2)) is True

    payment = due(plan, 2).payment
    assert payment.info_data["charged_currency"] == "EUR"

    refund = installments_order.refunds.create(
        payment=payment,
        provider="postfinance",
        amount=payment.amount,
        source=OrderRefund.REFUND_SOURCE_ADMIN,
        state=OrderRefund.REFUND_STATE_CREATED,
    )
    PostFinancePaymentProvider(alt_currency_event).execute_refund(refund)

    # pretix refunds 100.00 CHF; PostFinance is sent the 107.00 EUR charged.
    assert refund.amount == Decimal("100.00")
    assert postfinance.refunds[0]["amount"] == Decimal("107.00")


@pytest.mark.django_db
def test_a_declined_charge_leaves_a_failed_payment(
    installments_event, installments_order, pay_first_installment, postfinance
):
    """
    The decline is recorded on the order as a failed payment carrying
    PostFinance's reason, and does not count towards what has been paid.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(installments_event, first_payment_of(plan))

    postfinance.decline = "Insufficient funds"
    assert process_single_installment(due(plan, 2)) is False

    installment = due(plan, 2)
    assert installment.state == ScheduledInstallment.STATE_FAILED
    assert installment.failure_reason == "Insufficient funds"
    assert installment.payment.state == OrderPayment.PAYMENT_STATE_FAILED
    assert installment.payment.info_data["error"] == "Insufficient funds"
    assert installment.payment.info_data["transaction_id"]

    installments_order.refresh_from_db()
    assert installments_order.pending_sum == Decimal("200.00")

    plan.refresh_from_db()
    assert plan.status == InstallmentPlan.STATUS_ACTIVE
    assert plan.grace_period_end is not None


@pytest.mark.django_db
def test_a_webhook_for_an_automatic_charge_leaves_the_token_alone(
    installments_event, installments_order, pay_first_installment, postfinance, monkeypatch
):
    """
    A late webhook for an automatic charge must not write a token back onto
    a plan pretix has already finished and cleared.
    """
    plan = create_installment_plan(installments_order, "postfinance", 2)
    pay_first_installment(installments_event, first_payment_of(plan))
    assert process_single_installment(due(plan, 2)) is True

    plan.refresh_from_db()
    assert plan.status == InstallmentPlan.STATUS_COMPLETED
    assert plan.payment_token == {}

    charge_payment = due(plan, 2).payment
    transaction_id = charge_payment.info_data["transaction_id"]
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: fulfilled_transaction(),
    )
    monkeypatch.setattr(
        "pretix_postfinance.views._client_for_entity",
        lambda entity, space_id, entity_id, kind: (
            PostFinancePaymentProvider(installments_event)._get_client(),
            "ok",
        ),
    )

    with scopes_disabled():
        _process_transaction_webhook(transaction_id, 12345)

    plan.refresh_from_db()
    assert plan.payment_token == {}


@pytest.mark.django_db
def test_webhook_stores_the_token_when_the_customer_never_returns(
    installments_event, installments_order, monkeypatch, postfinance
):
    """
    A customer who closes the tab after paying still gets a working plan:
    the webhook is then the only place the token is ever seen.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)
    payment.info_data = {"transaction_id": 123456, "space_id": "12345"}
    payment.save(update_fields=["info"])

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: fulfilled_transaction(),
    )
    monkeypatch.setattr(
        "pretix_postfinance.views._client_for_entity",
        lambda entity, space_id, entity_id, kind: (
            PostFinancePaymentProvider(installments_event)._get_client(),
            "ok",
        ),
    )

    with scopes_disabled():
        status, processed = _process_transaction_webhook(123456, 12345)

    assert processed is True
    plan.refresh_from_db()
    assert plan.payment_token["token_id"] == TOKEN_ID
    assert plan.installments_paid == 1

    # The rest of the plan can now be charged.
    assert process_single_installment(due(plan, 2)) is True
    assert postfinance.token_charges()[0]["token"] == TOKEN_ID


@pytest.mark.django_db
def test_first_payment_stores_the_token_before_it_reaches_fulfill(
    installments_event, installments_order, pay_first_installment, postfinance
):
    """
    The token is kept even when the customer returns during AUTHORIZED.

    It exists from the moment the transaction is tokenized, and tying the
    store to FULFILL meant a customer who came back a second or two early
    left the plan with no token and no way to get one.
    """
    postfinance.transaction = fulfilled_transaction()
    postfinance.transaction.state = TransactionState.AUTHORIZED

    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)
    pay_first_installment(installments_event, payment)

    plan.refresh_from_db()
    assert plan.payment_token["token_id"] == TOKEN_ID
    assert plan.payment_token[METHOD_CONFIG_KEY] == METHOD_CONFIG_ID
    # AUTHORIZED is not money received, so the payment stays unconfirmed.
    assert payment.state != OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_first_payment_stores_no_token_when_the_transaction_failed(
    installments_event, installments_order, pay_first_installment, postfinance
):
    """A card that was declined is not worth keeping for later charges."""
    postfinance.transaction = fulfilled_transaction()
    postfinance.transaction.state = TransactionState.FAILED

    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(installments_event, first_payment_of(plan))

    plan.refresh_from_db()
    assert plan.payment_token == {}


@pytest.mark.django_db
def test_webhook_repairs_a_plan_whose_payment_was_confirmed_elsewhere(
    installments_event, installments_order, monkeypatch, postfinance
):
    """
    A payment settled by another route can still have its token recovered.

    An organizer marking a payment paid leaves the plan with no token, and the
    webhook used to return at the already-settled check before storing one,
    which left the plan permanently unchargeable.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)
    payment.info_data = {"transaction_id": 123456, "space_id": "12345"}
    payment.save(update_fields=["info"])
    payment.confirm()
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    plan.refresh_from_db()
    assert plan.payment_token == {}

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: fulfilled_transaction(),
    )
    monkeypatch.setattr(
        "pretix_postfinance.views._client_for_entity",
        lambda entity, space_id, entity_id, kind: (
            PostFinancePaymentProvider(installments_event)._get_client(),
            "ok",
        ),
    )

    with scopes_disabled():
        _process_transaction_webhook(123456, 12345)

    plan.refresh_from_db()
    assert plan.payment_token["token_id"] == TOKEN_ID

    # Which is the point: the remaining installments can now be charged.
    assert process_single_installment(due(plan, 2)) is True


@pytest.mark.django_db
def test_a_closed_plan_refuses_a_late_token(
    installments_event, installments_order, monkeypatch, postfinance
):
    """
    pretix revokes and clears the token when a plan closes, so a late webhook
    must not record another: it no longer exists upstream, and a plan holding
    it would only look chargeable.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)
    payment.info_data = {"transaction_id": 123456, "space_id": "12345"}
    payment.save(update_fields=["info"])
    plan.status = InstallmentPlan.STATUS_CANCELLED
    plan.save(update_fields=["status"])

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: fulfilled_transaction(),
    )
    monkeypatch.setattr(
        "pretix_postfinance.views._client_for_entity",
        lambda entity, space_id, entity_id, kind: (
            PostFinancePaymentProvider(installments_event)._get_client(),
            "ok",
        ),
    )

    with scopes_disabled():
        _process_transaction_webhook(123456, 12345)

    plan.refresh_from_db()
    assert plan.payment_token == {}


@pytest.mark.django_db
def test_missing_token_leaves_the_plan_chargeable_by_hand(
    installments_event, installments_order, pay_first_installment, postfinance
):
    """
    If PostFinance returns no token, the first payment still stands — only
    the scheduled installments cannot run.
    """
    plan = create_installment_plan(installments_order, "postfinance", 3)
    payment = first_payment_of(plan)

    postfinance.transaction = fulfilled_transaction(with_token=False)
    pay_first_installment(installments_event, payment)

    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    plan.refresh_from_db()
    assert plan.payment_token == {}

    installment = due(plan, 2)
    assert process_single_installment(installment) is False
    installment.refresh_from_db()
    assert installment.state == ScheduledInstallment.STATE_FAILED
    # pretix's own wording, not the provider's: with no token on the plan it
    # never gets as far as calling `execute_installment()`. The provider's
    # reason for the same situation is covered in `test_installments.py`.
    assert installment.failure_reason == "No payment token available"


@pytest.mark.django_db
def test_cancelling_a_plan_revokes_the_token(
    installments_event, installments_order, pay_first_installment, postfinance
):
    plan = create_installment_plan(installments_order, "postfinance", 3)
    pay_first_installment(installments_event, first_payment_of(plan))

    plan.refresh_from_db()
    cancel_installment_plan(plan, cancel_order=False)

    assert postfinance.revoked == [TOKEN_ID]
    plan.refresh_from_db()
    assert plan.status == InstallmentPlan.STATUS_CANCELLED
    assert plan.payment_token == {}
    assert set(plan.installments.values_list("state", flat=True)) == {
        ScheduledInstallment.STATE_PAID,
        ScheduledInstallment.STATE_CANCELLED,
    }


@pytest.mark.django_db
class TestALostResponseIsNotChargedTwice:
    """
    A charge is two calls to PostFinance and the money moves on the second. If
    the response to that second call is lost, the transaction is settled at
    PostFinance while pretix records the attempt as failed -- and pretix retries
    failed installments. Without a check, the retry takes the money again.
    """

    def _plan_with_a_lost_charge(self, event, order, pay_first_installment, postfinance):
        """Charge installment 2, losing the response after the money moved."""
        plan = create_installment_plan(order, "postfinance", 3)
        pay_first_installment(event, first_payment_of(plan))

        postfinance.lose_response = True
        assert process_single_installment(due(plan, 2)) is False
        postfinance.lose_response = False

        failed = due(plan, 2).payment
        assert failed.state == OrderPayment.PAYMENT_STATE_FAILED
        assert failed.info_data["transaction_id"]
        return plan, failed

    def test_the_retry_adopts_the_charge_instead_of_repeating_it(
        self, installments_event, installments_order, pay_first_installment, postfinance
    ):
        plan, failed = self._plan_with_a_lost_charge(
            installments_event, installments_order, pay_first_installment, postfinance
        )
        lost_transaction_id = failed.info_data["transaction_id"]

        # At PostFinance the charge did go through.
        postfinance.transaction = SimpleNamespace(
            id=lost_transaction_id,
            state=TransactionState.FULFILL,
            payment_connector_configuration=SimpleNamespace(name="TWINT"),
            created_on="2026-01-13T10:00:00Z",
        )
        before = len(postfinance.token_charges())

        assert process_single_installment(due(plan, 2)) is True

        assert len(postfinance.token_charges()) == before, "charged a second time"

        installment = due(plan, 2)
        assert installment.state == ScheduledInstallment.STATE_PAID
        assert installment.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        # The confirmed payment names the transaction that actually took the
        # money, so it can be reconciled and refunded.
        assert installment.payment.info_data["transaction_id"] == lost_transaction_id

        plan.refresh_from_db()
        assert plan.installments_paid == 2

        installments_order.refresh_from_db()
        assert installments_order.pending_sum == Decimal("100.00")

    def test_a_charge_that_really_failed_is_retried(
        self, installments_event, installments_order, pay_first_installment, postfinance
    ):
        """The guard must not turn a genuine decline into an uncollectable plan."""
        plan, failed = self._plan_with_a_lost_charge(
            installments_event, installments_order, pay_first_installment, postfinance
        )
        postfinance.transaction = SimpleNamespace(
            id=failed.info_data["transaction_id"],
            state=TransactionState.FAILED,
            payment_connector_configuration=None,
            created_on=None,
        )
        before = len(postfinance.token_charges())

        assert process_single_installment(due(plan, 2)) is True

        assert len(postfinance.token_charges()) == before + 1
        assert due(plan, 2).state == ScheduledInstallment.STATE_PAID

    def test_an_attempt_still_in_flight_is_left_alone(
        self, installments_event, installments_order, pay_first_installment, postfinance
    ):
        """A transaction that may yet settle must not be charged alongside."""
        plan, failed = self._plan_with_a_lost_charge(
            installments_event, installments_order, pay_first_installment, postfinance
        )
        postfinance.transaction = SimpleNamespace(
            id=failed.info_data["transaction_id"],
            state=TransactionState.PROCESSING,
            payment_connector_configuration=None,
            created_on=None,
        )
        before = len(postfinance.token_charges())

        assert process_single_installment(due(plan, 2)) is False

        assert len(postfinance.token_charges()) == before, "charged while one was in flight"
        installment = due(plan, 2)
        assert installment.state == ScheduledInstallment.STATE_FAILED
        assert installment.failure_reason == "An earlier payment attempt is still being processed."

    def test_an_uncheckable_attempt_is_left_alone(
        self, installments_event, installments_order, pay_first_installment,
        postfinance, monkeypatch,
    ):
        """If we cannot find out what happened, charging again is the wrong guess."""
        plan, _failed = self._plan_with_a_lost_charge(
            installments_event, installments_order, pay_first_installment, postfinance
        )
        def unreachable(self, tid):
            raise PostFinanceError("service unavailable", status_code=503)

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_transaction", unreachable
        )
        before = len(postfinance.token_charges())

        assert process_single_installment(due(plan, 2)) is False

        assert len(postfinance.token_charges()) == before
        assert due(plan, 2).failure_reason == "An earlier payment attempt could not be checked."

    def test_a_first_attempt_is_not_held_up_by_the_check(
        self, installments_event, installments_order, pay_first_installment, postfinance
    ):
        """The guard only looks at earlier attempts at the same installment."""
        plan = create_installment_plan(installments_order, "postfinance", 3)
        pay_first_installment(installments_event, first_payment_of(plan))
        before = len(postfinance.token_charges())

        assert process_single_installment(due(plan, 2)) is True

        assert len(postfinance.token_charges()) == before + 1
        assert due(plan, 2).state == ScheduledInstallment.STATE_PAID

    def test_a_later_installment_is_not_confused_with_an_earlier_failure(
        self, installments_event, installments_order, pay_first_installment, postfinance
    ):
        """Attempts are matched by installment, not merely by order."""
        plan, _failed = self._plan_with_a_lost_charge(
            installments_event, installments_order, pay_first_installment, postfinance
        )
        before = len(postfinance.token_charges())

        # Installment 3 has no history of its own and must charge normally,
        # even though installment 2 left a failed attempt on the same order.
        assert process_single_installment(due(plan, 3)) is True

        assert len(postfinance.token_charges()) == before + 1
        assert due(plan, 3).state == ScheduledInstallment.STATE_PAID
