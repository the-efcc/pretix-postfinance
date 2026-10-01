from __future__ import annotations

import json
import pathlib
from decimal import Decimal
from types import SimpleNamespace

import pytest
from postfinancecheckout.models import TokenizationMode, TransactionState
from pretix.base.models import InvoiceAddress, Order, OrderPayment, OrderRefund
from pretix.base.payment import PaymentException

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import (
    PostFinancePaymentProvider,
    PostFinanceProdSpacePaymentProvider,
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,expected_order_status,expected_payment_state",
    [
        (TransactionState.FULFILL, Order.STATUS_PAID, None),
        (
            TransactionState.COMPLETED,
            Order.STATUS_PENDING,
            OrderPayment.PAYMENT_STATE_PENDING,
        ),
        (
            TransactionState.AUTHORIZED,
            Order.STATUS_PENDING,
            OrderPayment.PAYMENT_STATE_PENDING,
        ),
        (TransactionState.FAILED, Order.STATUS_PENDING, OrderPayment.PAYMENT_STATE_FAILED),
        (TransactionState.DECLINE, Order.STATUS_PENDING, OrderPayment.PAYMENT_STATE_FAILED),
    ],
    ids=["fulfill", "completed", "authorized", "failed", "declined"],
)
def test_execute_payment_transaction_states(
    env, rf, monkeypatch, transaction_factory, state, expected_order_status, expected_payment_state
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(state=state),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": payment.pk,
    }
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == expected_order_status

    if expected_payment_state is not None:
        payment.refresh_from_db()
        assert payment.state == expected_payment_state


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,offers_token",
    [
        (TransactionState.FULFILL, True),
        (TransactionState.COMPLETED, True),
        (TransactionState.AUTHORIZED, True),
        (TransactionState.FAILED, False),
        (TransactionState.DECLINE, False),
        (TransactionState.VOIDED, False),
    ],
    ids=["fulfill", "completed", "authorized", "failed", "declined", "voided"],
)
def test_execute_payment_stores_the_token_before_fulfill(
    env, rf, monkeypatch, transaction_factory, state, offers_token
):
    """
    Keeping a token is not the same decision as confirming a payment.

    The token exists from the moment the transaction is tokenized, so it is
    stored for any state but a failure. Gating it on FULFILL meant a customer
    who returned while the transaction was still AUTHORIZED left the plan with
    no token, and nothing later could repair it. Asserted on the call so this
    also runs on an upstream pretix, which has no installments.
    """
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(state=state),
    )
    offered: list[int] = []
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider.store_installment_token",
        lambda self, payment, transaction: offered.append(payment.pk),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": payment.pk,
    }
    prov.execute_payment(req, payment)

    assert offered == ([payment.pk] if offers_token else [])


@pytest.mark.django_db
def test_execute_payment_api_error(env, rf, monkeypatch):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: (_ for _ in ()).throw(PostFinanceError("API Error", status_code=500)),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")

    payment = order.payments.create(provider="postfinance", amount=order.total)
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": payment.pk,
    }

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_execute_payment_no_transaction_id_creates_transaction(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=999888),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.execute_payment(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    payment.refresh_from_db()
    assert payment.info_data.get("pending_transaction_id") == 999888


@pytest.mark.django_db
def test_execute_payment_ignores_unrelated_session_transaction(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=999888),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")

    stale_payment = order.payments.create(provider="postfinance", amount=order.total)
    payment = order.payments.create(provider="postfinance", amount=order.total)
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": stale_payment.pk,
    }

    result = prov.execute_payment(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    payment.refresh_from_db()
    assert payment.info_data.get("pending_transaction_id") == 999888


@pytest.mark.django_db
def test_execute_payment_uses_order_line_items_for_new_checkout(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    captured_kwargs = {}
    captured_cart = {}
    expected_line_items = [object()]

    def capture_create_transaction(self, **kwargs):
        captured_kwargs.update(kwargs)
        return transaction_factory(id=999888)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        capture_create_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)

    def capture_line_items(cart, currency):
        captured_cart.update(
            {
                "positions": list(cart["positions"]),
                "fees": list(cart["fees"]),
                "total": cart["total"],
                "currency": currency,
            }
        )
        return expected_line_items

    monkeypatch.setattr(prov, "_build_line_items", capture_line_items)

    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.execute_payment(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert captured_kwargs["line_items"] is expected_line_items
    assert captured_cart == {
        "positions": [],
        "fees": [],
        "total": order.total,
        "currency": event.currency,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    (
        "name_parts",
        "expected_given_name",
        "expected_family_name",
        "expected_salutation",
    ),
    [
        (
            {
                "_scheme": "salutation_given_family",
                "salutation": "Ms",
                "given_name": "Ada",
                "family_name": "Lovelace",
            },
            "Ada",
            "Lovelace",
            "Ms",
        ),
        ({"_scheme": "full", "full_name": "Prince"}, "Prince", None, None),
    ],
    ids=["split-name", "full-name"],
)
def test_execute_payment_sends_customer_name_in_billing_address(
    env,
    rf,
    monkeypatch,
    transaction_factory,
    name_parts,
    expected_given_name,
    expected_family_name,
    expected_salutation,
):
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(self, **kwargs):
        captured_kwargs.update(kwargs)
        return transaction_factory(id=999888)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        capture_create_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    InvoiceAddress.objects.create(
        order=order,
        name_parts=name_parts,
        company="Analytical Engines Ltd",
        street="Main Street 1",
        zipcode="8000",
        city="Zurich",
        country="CH",
        country_old="CH",
        state="ZH",
        vat_id="CHE-123.456.789 VAT",
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.execute_payment(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    billing_address = captured_kwargs["billing_address"]
    assert billing_address is not None
    assert billing_address.given_name == expected_given_name
    assert billing_address.family_name == expected_family_name
    assert billing_address.salutation == expected_salutation
    assert billing_address.organization_name == "Analytical Engines Ltd"
    assert billing_address.email_address == order.email
    assert billing_address.street == "Main Street 1"
    assert billing_address.postcode == "8000"
    assert billing_address.city == "Zurich"
    assert billing_address.country == "CH"
    assert billing_address.postal_state == "ZH"
    assert billing_address.sales_tax_number == "CHE-123.456.789 VAT"


@pytest.mark.django_db
def test_refund_success(env, rf, monkeypatch, refund_factory):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: refund_factory(state="SUCCESSFUL", amount=13.37),
    )

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
    )

    prov.execute_refund(refund)

    refund.refresh_from_db()
    assert refund.state == OrderRefund.REFUND_STATE_TRANSIT


@pytest.mark.django_db
def test_refund_partial(env, rf, monkeypatch, refund_factory):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: refund_factory(state="SUCCESSFUL", amount=5.00),
    )

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    prov.execute_refund(refund)

    refund.refresh_from_db()
    assert refund.state == OrderRefund.REFUND_STATE_TRANSIT
    assert refund.info_data.get("refund_id") == 789012
    assert refund.info_data.get("state") == "SUCCESSFUL"


@pytest.mark.django_db
def test_refund_api_error(env, rf, monkeypatch):
    event, order = env

    def raise_refund_error(**kwargs):
        raise PostFinanceError("Refund failed", status_code=400)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: raise_refund_error(**kwargs),
    )

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
    )

    with pytest.raises(PaymentException):
        prov.execute_refund(refund)

    refund.refresh_from_db()
    assert refund.state != OrderRefund.REFUND_STATE_DONE
    assert refund.info_data.get("error") == "Refund failed"
    assert refund.info_data.get("error_status_code") == 400


@pytest.mark.django_db
def test_refund_wrong_state(env, rf):
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.AUTHORIZED.value,
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
    )

    with pytest.raises(PaymentException) as exc_info:
        prov.execute_refund(refund)

    assert "cannot be refunded" in str(exc_info.value)


@pytest.mark.django_db
def test_test_connection_success(env, monkeypatch, space_factory):
    event, _ = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: space_factory(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_auth_error(env, monkeypatch):
    event, _ = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: (_ for _ in ()).throw(PostFinanceError("Unauthorized", status_code=401)),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is False
    assert "Authentication failed" in message


@pytest.mark.django_db
def test_test_connection_missing_credentials(env):
    event, _ = env

    event.settings.set("payment_postfinance_space_id", "")
    event.settings.set("payment_postfinance_user_id", "")
    event.settings.set("payment_postfinance_auth_key", "")

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is False
    assert "configure" in message.lower()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,expected",
    [
        (TransactionState.COMPLETED.value, True),
        (TransactionState.FULFILL.value, True),
        (TransactionState.AUTHORIZED.value, False),
    ],
    ids=["completed", "fulfill", "authorized"],
)
def test_payment_refund_supported(env, state, expected):
    event, order = env

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"state": state}),
    )
    assert prov.payment_refund_supported(payment) is expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "session,expected",
    [
        ({"payment_postfinance_transaction_id": 123456}, True),
        ({}, True),
    ],
    ids=["with_transaction_id", "without_transaction_id_yet"],
)
def test_payment_is_valid_session(env, rf, session, expected):
    event, _ = env

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = session
    assert prov.payment_is_valid_session(req) is expected


@pytest.mark.django_db
def test_payment_is_valid_session_accepts_persisted_transaction(env, rf):
    event, order = env

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"pending_transaction_id": 654321}),
    )

    req = rf.get("/")
    req.session = {}
    req.resolver_match = SimpleNamespace(kwargs={"payment": payment.pk})

    assert prov.payment_is_valid_session(req) is True


@pytest.mark.django_db
def test_payment_prepare_persists_transaction_on_payment(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=999888),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/", {"payment": "postfinance"})
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.payment_prepare(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert req.session.get("payment_postfinance_transaction_id") == 999888
    assert req.session.get("payment_postfinance_transaction_payment_id") == payment.pk
    payment.refresh_from_db()
    assert payment.info_data.get("pending_transaction_id") == 999888


@pytest.mark.django_db
def test_payment_prepare_cleans_stale_payment_transaction_on_failure(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=999888),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: None,
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/", {"payment": "postfinance"})
    req.session = {}

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"pending_transaction_id": 123456, "other": "keep"}),
    )
    result = prov.payment_prepare(req, payment)

    assert result is False
    assert "payment_postfinance_transaction_id" not in req.session
    assert "payment_postfinance_transaction_payment_id" not in req.session
    payment.refresh_from_db()
    assert payment.info_data.get("pending_transaction_id") is None
    assert payment.info_data.get("other") == "keep"


@pytest.mark.django_db
def test_payment_prepare_uses_installment_reference_and_tokenization(
    env, rf, monkeypatch, transaction_factory
):
    """
    The first payment of an installment plan asks PostFinance for a reusable
    token, which is what the later installments are charged against.
    """
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(self, **kwargs):
        captured_kwargs.update(kwargs)
        return transaction_factory(id=999888)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        capture_create_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.scheduled_installment_for_payment",
        lambda payment: SimpleNamespace(
            installment_number=1,
            plan=SimpleNamespace(pk=42, total_installments=3),
        ),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/", {"payment": "postfinance"})
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=Decimal("100.00"))

    result = prov.payment_prepare(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert captured_kwargs["merchant_reference"] == f"{event.slug}-{order.code}-inst-1"
    assert captured_kwargs["tokenization_mode"] == TokenizationMode.FORCE_CREATION
    assert captured_kwargs["line_items"][0].name == (
        f"Installment 1 of 3 for order {order.code}"
    )


@pytest.mark.django_db
def test_execute_payment_uses_persisted_transaction_without_session(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(id=tid, state=TransactionState.FULFILL),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"pending_transaction_id": 123456}),
    )
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    assert payment.info_data.get("transaction_id") == 123456
    assert payment.info_data.get("pending_transaction_id") is None


@pytest.mark.django_db
def test_matching_id(env):
    event, order = env

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )

    assert prov.matching_id(payment) == 123456


@pytest.mark.django_db
def test_shred_payment_info(env):
    event, order = env

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
                "payment_method": "TWINT",
                "created_on": "2026-01-13T10:00:00Z",
            }
        ),
    )

    prov.shred_payment_info(payment)

    payment.refresh_from_db()
    info = payment.info_data
    assert info.get("transaction_id") == 123456
    assert info.get("state") == TransactionState.COMPLETED.value
    assert info.get("_shredded") is True
    assert info.get("payment_method") is None
    assert info.get("created_on") is None


@pytest.mark.django_db
def test_shred_payment_info_drops_the_token_id(env):
    event, order = env

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
                "installment_charge": True,
                "token_id": 42,
            }
        ),
    )

    prov.shred_payment_info(payment)

    payment.refresh_from_db()
    info = payment.info_data
    # A handle to the customer's stored payment method is personal data
    assert info.get("token_id") is None
    # ... but the flag that keeps a late webhook from writing a token back
    # onto the plan is not, and the guard needs it
    assert info.get("installment_charge") is True


@pytest.mark.django_db
def test_shred_refund_info_keeps_external_references(env):
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    prov = PostFinancePaymentProvider(event)

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps(
            {
                "refund_id": 789012,
                "state": "SUCCESSFUL",
                "amount": 13.37,
                "created_on": "2026-01-13T11:00:00Z",
            }
        ),
    )

    prov.shred_payment_info(refund)

    refund.refresh_from_db()
    info = refund.info_data
    assert info.get("refund_id") == 789012
    assert info.get("state") == "SUCCESSFUL"
    assert info.get("amount") == 13.37
    assert info.get("_shredded") is True
    assert info.get("created_on") is None
    # Still matchable against PostFinance's own records
    assert prov.refund_matching_id(refund) == "789012"


@pytest.mark.django_db
def test_api_refund_details(env):
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )

    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps(
            {
                "refund_id": 789012,
                "state": "SUCCESSFUL",
                "amount": 13.37,
                "created_on": "2026-01-13T11:00:00Z",
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    details = prov.api_refund_details(refund)

    assert details["refund_id"] == 789012
    assert details["state"] == "SUCCESSFUL"
    assert details["amount"] == 13.37
    assert details["created_on"] == "2026-01-13T11:00:00Z"


@pytest.mark.django_db
def test_api_refund_details_with_error(env):
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )

    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps(
            {
                "refund_id": 789012,
                "state": "FAILED",
                "error": "Refund rejected",
                "error_code": "INSUFFICIENT_FUNDS",
                "error_status_code": 400,
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    details = prov.api_refund_details(refund)

    assert details["refund_id"] == 789012
    assert details["state"] == "FAILED"
    assert details["error"] == "Refund rejected"
    assert details["error_code"] == "INSUFFICIENT_FUNDS"
    assert details["error_status_code"] == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    "refund_info,expected",
    [
        ({"refund_id": 789012}, "PostFinance (789012)"),
        ({}, "PostFinance"),
    ],
    ids=["with_refund_id", "without_refund_id"],
)
def test_refund_control_render_short(env, refund_info, expected):
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )

    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps(refund_info),
    )

    prov = PostFinancePaymentProvider(event)
    assert prov.refund_control_render_short(refund) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error_type,exception",
    [
        ("success", None),
        ("api_error", PostFinanceError("API Error", status_code=500)),
        ("generic_error", RuntimeError("Unexpected error")),
    ],
    ids=["success", "api_error", "generic_exception"],
)
def test_execute_payment_cleans_session(
    env, rf, monkeypatch, transaction_factory, error_type, exception
):
    event, order = env

    if exception:
        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_transaction",
            lambda self, tid: (_ for _ in ()).throw(exception),
        )
    else:
        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_transaction",
            lambda self, tid: transaction_factory(state=TransactionState.COMPLETED),
        )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")

    payment = order.payments.create(provider="postfinance", amount=order.total)
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": payment.pk,
    }

    if exception:
        with pytest.raises(PaymentException):
            prov.execute_payment(req, payment)
    else:
        prov.execute_payment(req, payment)

    assert "payment_postfinance_transaction_id" not in req.session
    assert "payment_postfinance_transaction_payment_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_clears_stale_session_without_creating_transaction(
    env, rf, monkeypatch
):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: pytest.fail("checkout_prepare should not create a transaction"),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/", {"payment": "postfinance"})
    req.session = {
        "payment_postfinance_transaction_id": 123456,
        "payment_postfinance_transaction_payment_id": 999,
    }
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result is True
    assert "payment_postfinance_transaction_id" not in req.session
    assert "payment_postfinance_transaction_payment_id" not in req.session


@pytest.mark.django_db
def test_api_payment_details(env):
    event, order = env

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
                "payment_method": "TWINT",
                "created_on": "2026-01-13T10:00:00Z",
            }
        ),
    )

    prov = PostFinancePaymentProvider(event)
    details = prov.api_payment_details(payment)

    assert details["transaction_id"] == 123456
    assert details["state"] == TransactionState.COMPLETED.value
    assert details["payment_method"] == "TWINT"
    assert details["created_on"] == "2026-01-13T10:00:00Z"


@pytest.mark.django_db
def test_api_payment_details_empty_info(env):
    event, order = env

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({}),
    )

    prov = PostFinancePaymentProvider(event)
    details = prov.api_payment_details(payment)

    assert details["transaction_id"] is None
    assert details["state"] is None
    assert details["payment_method"] is None
    assert details["created_on"] is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "test_creds,expected",
    [
        ({}, False),
        (
            {
                "payment_postfinance_test_space_id": "99999",
                "payment_postfinance_test_user_id": "88888",
                "payment_postfinance_test_auth_key": "test-secret",
            },
            True,
        ),
        ({"payment_postfinance_test_space_id": "99999"}, False),
    ],
    ids=["not_configured", "fully_configured", "partial"],
)
def test_has_test_credentials(env, test_creds, expected):
    event, _ = env
    for key, value in test_creds.items():
        event.settings.set(key, value)

    prov = PostFinancePaymentProvider(event)
    assert prov._has_test_credentials() is expected


@pytest.mark.django_db
def test_get_credentials_returns_live_when_not_testmode(env):
    event, _ = env
    event.testmode = False
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    prov = PostFinancePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "12345"
    assert user_id == "67890"
    assert auth_key == "test-secret"


@pytest.mark.django_db
def test_get_credentials_returns_live_when_testmode_but_no_test_creds(testmode_env):
    event, _ = testmode_env
    prov = PostFinancePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "12345"
    assert user_id == "67890"
    assert auth_key == "live-secret"


@pytest.mark.django_db
def test_get_credentials_returns_test_when_testmode_with_test_creds(testmode_env):
    event, _ = testmode_env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    prov = PostFinancePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "99999"
    assert user_id == "88888"
    assert auth_key == "test-secret"


@pytest.mark.django_db
def test_test_mode_message_with_test_credentials(testmode_env):
    event, _ = testmode_env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    prov = PostFinancePaymentProvider(event)
    message = prov.test_mode_message

    assert "test credentials" in message.lower()
    assert "no real charges" in message.lower()


@pytest.mark.django_db
def test_test_mode_message_without_test_credentials(testmode_env):
    event, _ = testmode_env
    prov = PostFinancePaymentProvider(event)
    message = prov.test_mode_message

    assert "live credentials" in message.lower()
    assert "no test credentials" in message.lower()


@pytest.mark.django_db
def test_get_client_uses_test_credentials_in_testmode(testmode_env, monkeypatch):
    event, _ = testmode_env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    captured_args = {}

    def mock_init(self, space_id, user_id, api_secret):
        captured_args["space_id"] = space_id
        captured_args["user_id"] = user_id
        captured_args["api_secret"] = api_secret

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.__init__",
        mock_init,
    )

    prov = PostFinancePaymentProvider(event)
    prov._get_client()

    assert captured_args["space_id"] == 99999
    assert captured_args["user_id"] == 88888
    assert captured_args["api_secret"] == "test-secret"


@pytest.mark.django_db
def test_get_client_uses_live_credentials_when_not_testmode(env, monkeypatch):
    event, _ = env
    event.testmode = False
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    captured_args = {}

    def mock_init(self, space_id, user_id, api_secret):
        captured_args["space_id"] = space_id
        captured_args["user_id"] = user_id
        captured_args["api_secret"] = api_secret

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.__init__",
        mock_init,
    )

    prov = PostFinancePaymentProvider(event)
    prov._get_client()

    assert captured_args["space_id"] == 12345
    assert captured_args["user_id"] == 67890
    assert captured_args["api_secret"] == "test-secret"


@pytest.mark.django_db
def test_test_connection_indicates_test_mode(testmode_env, monkeypatch, space_factory):
    event, _ = testmode_env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: space_factory(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "test" in message.lower()
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_indicates_live_mode(env, monkeypatch, space_factory):
    event, _ = env
    event.testmode = False

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: space_factory(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "live" in message.lower()
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_explicit_mode_targets_correct_space(env, monkeypatch):
    event, _ = env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret-test")

    captured = {}

    def mock_init(self, space_id, user_id, api_secret):
        captured["space_id"] = space_id
        captured["user_id"] = user_id
        captured["api_secret"] = api_secret

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.__init__", mock_init
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: type("S", (), {"name": "X"})(),
    )

    prov = PostFinancePaymentProvider(event)
    prov.test_connection(mode="test")
    assert captured["space_id"] == 99999

    captured.clear()
    prov.test_connection(mode="live")
    assert captured["space_id"] == 12345


@pytest.mark.django_db
def test_test_connection_test_mode_missing_credentials(env):
    event, _ = env
    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection(mode="test")

    assert success is False
    assert "test" in message.lower()


@pytest.mark.django_db
def test_setup_webhooks_targets_correct_space(env, monkeypatch):
    event, _ = env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret-test")

    captured = {}

    def mock_init(self, space_id, user_id, api_secret):
        captured["space_id"] = space_id

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.__init__", mock_init
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.setup_webhooks",
        lambda self, url: {
            "created_transaction_listener": True,
            "created_refund_listener": True,
        },
    )

    prov = PostFinancePaymentProvider(event)
    success, _msg = prov.setup_webhooks("https://example.com/webhook", mode="test")
    assert success is True
    assert captured["space_id"] == 99999

    captured.clear()
    prov.setup_webhooks("https://example.com/webhook", mode="live")
    assert captured["space_id"] == 12345


@pytest.mark.django_db
def test_setup_webhooks_test_mode_missing_credentials(env):
    event, _ = env
    prov = PostFinancePaymentProvider(event)
    success, message = prov.setup_webhooks("https://example.com/webhook", mode="test")

    assert success is False
    assert "test" in message.lower()


# Production space provider (test mode)


def _set_test_credentials(event):
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")


def _offer_prod_space(event):
    event.settings.set("payment_postfinance_prod_space_in_testmode", True)


@pytest.mark.django_db
def test_both_providers_registered(env):
    event, _ = env
    providers = event.get_payment_providers()

    assert "postfinance" in providers
    assert "postfinance_prod" in providers
    assert isinstance(providers["postfinance_prod"], PostFinanceProdSpacePaymentProvider)


@pytest.mark.django_db
def test_prod_provider_shares_settings_with_main_provider(env):
    event, _ = env
    prov = PostFinanceProdSpacePaymentProvider(event)

    assert prov.settings.get("space_id") == "12345"
    assert prov.is_enabled is True


@pytest.mark.django_db
def test_prod_provider_has_no_own_settings_section(env, rf):
    event, _ = env
    prov = PostFinanceProdSpacePaymentProvider(event)

    assert prov.settings_form_fields == {}
    assert prov.settings_content_render(rf.get("/")) == ""


@pytest.mark.django_db
def test_prod_provider_uses_live_credentials_in_testmode(testmode_env):
    event, _ = testmode_env
    _set_test_credentials(event)

    prov = PostFinanceProdSpacePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "12345"
    assert user_id == "67890"
    assert auth_key == "live-secret"


@pytest.mark.django_db
def test_prod_provider_client_targets_live_space_in_testmode(testmode_env, monkeypatch):
    event, _ = testmode_env
    _set_test_credentials(event)

    captured = {}

    def mock_init(self, space_id, user_id, api_secret):
        captured["space_id"] = space_id

    monkeypatch.setattr("pretix_postfinance.payment.PostFinanceClient.__init__", mock_init)

    prov = PostFinanceProdSpacePaymentProvider(event)
    prov._get_client()

    assert captured["space_id"] == 12345


@pytest.mark.django_db
def test_prod_provider_not_allowed_outside_testmode(env, monkeypatch):
    event, _ = env
    event.testmode = False
    _set_test_credentials(event)

    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.is_allowed",
        lambda self, request, total=None: True,
    )

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.is_allowed(request=None) is False


@pytest.mark.django_db
def test_prod_provider_not_allowed_without_test_credentials(testmode_env, monkeypatch):
    event, _ = testmode_env

    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.is_allowed",
        lambda self, request, total=None: True,
    )

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.is_allowed(request=None) is False


@pytest.mark.django_db
def test_prod_provider_allowed_in_testmode_with_test_credentials(testmode_env, monkeypatch):
    event, _ = testmode_env
    _set_test_credentials(event)
    _offer_prod_space(event)

    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.is_allowed",
        lambda self, request, total=None: True,
    )

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.is_allowed(request=None) is True


@pytest.mark.django_db
def test_prod_provider_public_name_has_production_space_suffix(testmode_env):
    event, _ = testmode_env
    _set_test_credentials(event)

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.public_name == "PostFinance (production space)"


@pytest.mark.django_db
def test_prod_provider_public_name_uses_custom_display_name(testmode_env):
    event, _ = testmode_env
    _set_test_credentials(event)
    event.settings.set("payment_postfinance_public_name", "Swiss Payments")

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.public_name == "Swiss Payments (production space)"


@pytest.mark.django_db
def test_main_provider_public_name_has_test_space_suffix_in_testmode(testmode_env):
    event, _ = testmode_env
    _set_test_credentials(event)
    _offer_prod_space(event)

    prov = PostFinancePaymentProvider(event)
    assert prov.public_name == "PostFinance (test space)"


@pytest.mark.django_db
def test_main_provider_public_name_unchanged_without_test_credentials(testmode_env):
    event, _ = testmode_env

    prov = PostFinancePaymentProvider(event)
    assert prov.public_name == "PostFinance"


@pytest.mark.django_db
def test_main_provider_public_name_unchanged_outside_testmode(env):
    event, _ = env
    event.testmode = False
    _set_test_credentials(event)

    prov = PostFinancePaymentProvider(event)
    assert prov.public_name == "PostFinance"


@pytest.mark.django_db
def test_prod_provider_test_mode_message_warns_about_real_charges(testmode_env):
    event, _ = testmode_env
    _set_test_credentials(event)

    prov = PostFinanceProdSpacePaymentProvider(event)
    message = prov.test_mode_message

    assert "production" in message.lower()
    assert "real" in message.lower()


@pytest.mark.django_db
def test_space_id_for_payment(env):
    event, order = env
    _set_test_credentials(event)

    payment = order.payments.create(provider="postfinance", amount=order.total)

    # Live order -> live space for both providers
    assert PostFinancePaymentProvider(event)._space_id_for_payment(payment) == "12345"
    assert PostFinanceProdSpacePaymentProvider(event)._space_id_for_payment(payment) == "12345"

    # Test mode order -> test space for the main provider, live space for
    # the production space provider
    order.testmode = True
    order.save()
    assert PostFinancePaymentProvider(event)._space_id_for_payment(payment) == "99999"
    assert PostFinanceProdSpacePaymentProvider(event)._space_id_for_payment(payment) == "12345"


@pytest.mark.django_db
def test_recorded_space_wins_over_the_events_current_test_mode(env):
    """The space a transaction was created in does not change when the event
    later leaves test mode, so the recorded space is authoritative."""
    event, order = env
    _set_test_credentials(event)

    order.testmode = True
    order.save()
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 1, "space_id": 99999}),
    )

    prov = PostFinancePaymentProvider(event)
    assert prov._space_id_for_payment(payment) == "99999"
    assert prov._mode_for_payment(payment) == "test"

    # Event goes live; the transaction still lives in the test space
    event.testmode = False
    order.testmode = False
    order.save()
    prov = PostFinancePaymentProvider(event)
    assert prov._space_id_for_payment(payment) == "99999"
    assert prov._mode_for_payment(payment) == "test"


@pytest.mark.django_db
def test_refund_targets_the_space_the_payment_was_created_in(env, monkeypatch, refund_factory):
    """A refund must be issued in the space holding the transaction, not the
    one the event's current test mode points at."""
    event, order = env
    _set_test_credentials(event)

    captured = {}

    def refund_transaction(self, **kwargs):
        captured["space_id"] = self.space_id
        return refund_factory(state="SUCCESSFUL", amount=13.37)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction", refund_transaction
    )

    order.status = Order.STATUS_PAID
    order.save()
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "space_id": 99999,
                "state": TransactionState.COMPLETED.value,
            }
        ),
    )
    refund = order.refunds.create(provider="postfinance", amount=order.total, payment=payment)

    # The event is not in test mode any more, but the transaction is still
    # in the test space
    PostFinancePaymentProvider(event).execute_refund(refund)

    assert captured["space_id"] == 99999
    assert refund.info_data["space_id"] == 99999


@pytest.mark.django_db
def test_transaction_creation_records_its_space(testmode_env, monkeypatch, transaction_factory):
    event, order = testmode_env
    _set_test_credentials(event)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=555),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: "https://pay.example/555",
    )

    payment = order.payments.create(provider="postfinance", amount=order.total)

    transaction_id, url, space_id = PostFinancePaymentProvider(
        event
    )._create_payment_transaction(payment)

    assert transaction_id == 555
    assert space_id == 99999

    # And the production space provider records the live space
    _, _, prod_space_id = PostFinanceProdSpacePaymentProvider(
        event
    )._create_payment_transaction(payment)
    assert prod_space_id == 12345


@pytest.mark.django_db
def test_prod_provider_not_offered_for_payment_change_outside_testmode(env):
    """is_allowed() only gates checkout; retrying or changing the payment
    method of an existing order goes through order_change_allowed()."""
    event, order = env
    event.testmode = False
    _set_test_credentials(event)

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.order_change_allowed(order) is False


@pytest.mark.django_db
def test_prod_provider_not_offered_for_payment_change_without_test_credentials(testmode_env):
    event, order = testmode_env

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.order_change_allowed(order) is False


@pytest.mark.django_db
def test_prod_provider_offered_for_payment_change_in_testmode(testmode_env, monkeypatch):
    event, order = testmode_env
    _set_test_credentials(event)
    _offer_prod_space(event)

    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.order_change_allowed",
        lambda self, order, request=None: True,
    )

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.order_change_allowed(order) is True


@pytest.mark.django_db
def test_prod_provider_not_offered_unless_switched_on(testmode_env, monkeypatch):
    """The production space option is off unless an organizer switches it on
    in the payment settings, which access to may be restricted."""
    event, order = testmode_env
    _set_test_credentials(event)

    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.is_allowed",
        lambda self, request, total=None: True,
    )
    monkeypatch.setattr(
        "pretix.base.payment.BasePaymentProvider.order_change_allowed",
        lambda self, order, request=None: True,
    )

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.is_allowed(request=None) is False
    assert prov.order_change_allowed(order) is False

    _offer_prod_space(event)

    prov = PostFinanceProdSpacePaymentProvider(event)
    assert prov.is_allowed(request=None) is True
    assert prov.order_change_allowed(order) is True


@pytest.mark.django_db
def test_main_provider_public_name_unchanged_unless_prod_space_offered(testmode_env):
    """Without a second option to tell it apart from, the main provider keeps
    its plain name."""
    event, _ = testmode_env
    _set_test_credentials(event)

    assert PostFinancePaymentProvider(event).public_name == "PostFinance"

    _offer_prod_space(event)
    assert PostFinancePaymentProvider(event).public_name == "PostFinance (test space)"


@pytest.mark.django_db
def test_prod_space_option_is_in_the_main_providers_settings(env):
    """The switch belongs to the main provider's settings section; the
    production space provider has none of its own."""
    event, _ = env

    fields = PostFinancePaymentProvider(event).settings_form_fields
    assert "prod_space_in_testmode" in fields
    assert fields["prod_space_in_testmode"].required is False
    assert PostFinanceProdSpacePaymentProvider(event).settings_form_fields == {}


def test_control_templates_carry_no_inline_styles():
    """pretix serves the control panel under `style-src 'self'`.

    Inline `style=` attributes are silently dropped there, so a template
    that colours an error red with one renders plain black. Bootstrap
    classes are the only styling that survives.
    """
    templates = (
        pathlib.Path(__file__).parent.parent
        / "pretix_postfinance"
        / "templates"
        / "pretixplugins"
        / "postfinance"
    )
    offenders = [
        path.name for path in templates.glob("*.html") if 'style="' in path.read_text()
    ]
    assert not offenders


@pytest.mark.django_db
def test_execute_payment_failure_keeps_transaction_reference(
    env, rf, monkeypatch, transaction_factory
):
    """
    `OrderPayment.fail()` assigns what it is given to info_data instead of
    merging it, so failing a payment must pass the existing data back in.
    Losing it would take the dashboard link, `matching_id()` and the FX
    snapshot with it, and leave no transaction_id for a later webhook to
    match on.
    """
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(id=tid, state=TransactionState.FAILED),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "pending_transaction_id": 424242,
                "space_id": 12345,
                "fx_rate": "1.08",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "14.44",
            }
        ),
    )

    prov.execute_payment(req, payment)

    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED
    assert payment.info_data["state"] == "FAILED"
    assert payment.info_data["transaction_id"] == 424242
    assert payment.info_data["space_id"] == 12345
    assert payment.info_data["charged_currency"] == "CHF"
    assert payment.info_data["charged_amount"] == "14.44"
    assert prov.matching_id(payment) == 424242



@pytest.mark.django_db
def test_refund_external_id_is_scoped_to_the_organizer(env, monkeypatch, refund_factory):
    """
    PostFinance will not execute a second refund carrying an external ID it
    has already seen. Event slugs are unique per organizer, not globally, so
    an ID built without the organizer can collide between two organizers
    sharing a space — and the second refund is quietly skipped.
    """
    event, order = env

    seen = {}

    def fake_refund(self, **kwargs):
        seen.update(kwargs)
        return refund_factory()

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction", fake_refund
    )

    prov = PostFinancePaymentProvider(event)
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456, "state": "FULFILL"}),
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
    )
    refund = order.refunds.create(
        provider="postfinance",
        payment=payment,
        amount=order.total,
        state=OrderRefund.REFUND_STATE_CREATED,
    )

    prov.execute_refund(refund)

    assert seen["external_id"].startswith(f"pretix-{event.organizer.slug}-{event.slug}-")
    assert event.organizer.slug in seen["external_id"]


@pytest.mark.django_db
def test_two_organizers_with_the_same_event_slug_do_not_collide(
    organizer, testmode_organizer, monkeypatch, refund_factory
):
    """The collision the organizer segment exists to prevent."""
    from datetime import timedelta

    from django.utils.timezone import now
    from pretix.base.models import Event, Order

    ids = []

    def fake_refund(self, **kwargs):
        ids.append(kwargs["external_id"])
        return refund_factory()

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction", fake_refund
    )

    for owner in (organizer, testmode_organizer):
        event = Event.objects.create(
            organizer=owner,
            name="Annual",
            slug="annual",
            date_from=now(),
            live=True,
            plugins="pretix_postfinance",
        )
        event.settings.set("payment_postfinance_space_id", "12345")
        event.settings.set("payment_postfinance_user_id", "67890")
        event.settings.set("payment_postfinance_auth_key", "secret")
        order = Order.objects.create(
            code="SAMECODE",
            event=event,
            email="dummy@dummy.test",
            status=Order.STATUS_PAID,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("10.00"),
            sales_channel=owner.sales_channels.get(identifier="web"),
        )
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 1, "state": "FULFILL"}),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        )
        refund = order.refunds.create(
            provider="postfinance",
            payment=payment,
            amount=order.total,
            state=OrderRefund.REFUND_STATE_CREATED,
        )
        PostFinancePaymentProvider(event).execute_refund(refund)

    assert len(ids) == 2
    assert ids[0] != ids[1]
