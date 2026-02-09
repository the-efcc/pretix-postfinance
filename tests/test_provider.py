"""
Tests for the PostFinance payment provider.

Inspired by pretix's Stripe plugin test suite.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scope
from postfinancecheckout.models import TransactionState
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Organizer
from pretix.base.payment import PaymentException

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import PostFinancePaymentProvider


@pytest.fixture
def env():
    """Create test environment with organizer, event, and order."""
    o = Organizer.objects.create(name="Dummy", slug="dummy")
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o,
            name="Dummy",
            slug="dummy",
            date_from=now(),
            live=True,
            plugins="pretix_postfinance",
        )
        event.settings.set("payment_postfinance_space_id", "12345")
        event.settings.set("payment_postfinance_user_id", "67890")
        event.settings.set("payment_postfinance_auth_key", "test-secret")

        event.settings.set("payment_postfinance__enabled", True)

        order = Order.objects.create(
            code="FOOBAR",
            event=event,
            email="dummy@dummy.test",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("13.37"),
            sales_channel=o.sales_channels.get(identifier="web"),
        )
        yield event, order


@pytest.fixture(autouse=True)
def no_messages(monkeypatch):
    """Patch out template rendering for performance improvements."""
    monkeypatch.setattr("django.contrib.messages.api.add_message", lambda *args, **kwargs: None)


@pytest.fixture
def factory():
    """Create request factory."""
    return RequestFactory()


class MockedTransaction:
    """Mock PostFinance Transaction object."""

    id = 123456
    state = TransactionState.COMPLETED
    payment_connector_configuration = MagicMock()
    payment_connector_configuration.name = "TWINT"
    created_on = "2026-01-13T10:00:00Z"


class MockedRefund:
    """Mock PostFinance Refund object."""

    id = 789012
    state = MagicMock()
    state.value = "SUCCESSFUL"
    amount = 50.00
    created_on = "2026-01-13T11:00:00Z"


class MockedSpace:
    """Mock PostFinance Space object."""

    id = 12345
    name = "Test Space"


class MockedCompletion:
    """Mock PostFinance TransactionCompletion object."""

    id = 111222


class MockedVoid:
    """Mock PostFinance TransactionVoid object."""

    id = 333444


@pytest.mark.django_db
def test_perform_success(env, factory, monkeypatch):
    """Test successful payment execution."""
    event, order = env

    def get_transaction(transaction_id):
        t = MockedTransaction()
        t.state = TransactionState.COMPLETED
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_perform_authorized_state_pending(env, factory, monkeypatch):
    """Test AUTHORIZED state sets payment to pending (not confirmed - funds not captured yet)."""
    event, order = env

    def get_transaction(transaction_id):
        t = MockedTransaction()
        t.state = TransactionState.AUTHORIZED
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_perform_failed(env, factory, monkeypatch):
    """Test failed payment execution."""
    event, order = env

    def get_transaction(transaction_id):
        t = MockedTransaction()
        t.state = TransactionState.FAILED
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_perform_declined(env, factory, monkeypatch):
    """Test declined payment execution."""
    event, order = env

    def get_transaction(transaction_id):
        t = MockedTransaction()
        t.state = TransactionState.DECLINE
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_perform_api_error(env, factory, monkeypatch):
    """Test payment execution with API error."""
    event, order = env

    def get_transaction_error(transaction_id):
        raise PostFinanceError("API Error", status_code=500)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction_error(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_perform_no_transaction_id(env, factory):
    """Test payment execution without transaction ID in session."""
    event, order = env

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.execute_payment(req, payment)

    # Should return None without raising exception
    assert result is None
    payment.refresh_from_db()
    assert payment.info_data.get("error") == "No transaction ID in session"


@pytest.mark.django_db
def test_refund_success(env, factory, monkeypatch):
    """Test successful refund execution."""
    event, order = env

    def refund_transaction(*args, **kwargs):
        r = MockedRefund()
        r.id = 789012
        r.state = MagicMock()
        r.state.value = "SUCCESSFUL"
        r.amount = 13.37
        r.created_on = "2026-01-13T11:00:00Z"
        return r

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: refund_transaction(**kwargs),
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
def test_refund_partial(env, factory, monkeypatch):
    """Test partial refund execution."""
    event, order = env

    def refund_transaction(*args, **kwargs):
        r = MockedRefund()
        r.id = 789012
        r.state = MagicMock()
        r.state.value = "SUCCESSFUL"
        r.amount = 5.00
        r.created_on = "2026-01-13T11:00:00Z"
        return r

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: refund_transaction(**kwargs),
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
    # Refund info is stored on the refund object
    assert refund.info_data.get("refund_id") == 789012
    assert refund.info_data.get("state") == "SUCCESSFUL"


@pytest.mark.django_db
def test_refund_api_error(env, factory, monkeypatch):
    """Test refund with API error."""
    event, order = env

    def refund_error(*args, **kwargs):
        raise PostFinanceError("Refund failed", status_code=400)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        lambda self, **kwargs: refund_error(**kwargs),
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
    # Verify error details are stored in refund.info
    assert refund.info_data.get("error") == "Refund failed"
    assert refund.info_data.get("error_status_code") == 400


@pytest.mark.django_db
def test_refund_wrong_state(env, factory):
    """Test refund when transaction is not in refundable state."""
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.AUTHORIZED.value,  # Not refundable
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
def test_test_connection_success(env, monkeypatch):
    """Test successful connection test."""
    event, _ = env

    def get_space():
        return MockedSpace()

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: get_space(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_auth_error(env, monkeypatch):
    """Test connection test with authentication error."""
    event, _ = env

    def get_space_error():
        raise PostFinanceError("Unauthorized", status_code=401)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: get_space_error(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is False
    assert "Authentication failed" in message


@pytest.mark.django_db
def test_test_connection_missing_credentials(env):
    """Test connection test with missing credentials."""
    event, _ = env

    # Clear credentials
    event.settings.set("payment_postfinance_space_id", "")
    event.settings.set("payment_postfinance_user_id", "")
    event.settings.set("payment_postfinance_auth_key", "")

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is False
    assert "configure" in message.lower()


@pytest.mark.django_db
def test_payment_refund_supported(env):
    """Test payment_refund_supported returns correct value."""
    event, order = env

    prov = PostFinancePaymentProvider(event)

    # Should be supported for COMPLETED state
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"state": TransactionState.COMPLETED.value}),
    )
    assert prov.payment_refund_supported(payment) is True

    # Should be supported for FULFILL state
    payment2 = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"state": TransactionState.FULFILL.value}),
    )
    assert prov.payment_refund_supported(payment2) is True

    # Should not be supported for AUTHORIZED state
    payment3 = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"state": TransactionState.AUTHORIZED.value}),
    )
    assert prov.payment_refund_supported(payment3) is False


@pytest.mark.django_db
def test_payment_is_valid_session(env, factory):
    """Test payment_is_valid_session checks for transaction ID."""
    event, _ = env

    prov = PostFinancePaymentProvider(event)

    # Valid session with transaction ID
    req = factory.get("/")
    req.session = {"payment_postfinance_transaction_id": 123456}
    assert prov.payment_is_valid_session(req) is True

    # Invalid session without transaction ID
    req2 = factory.get("/")
    req2.session = {}
    assert prov.payment_is_valid_session(req2) is False


@pytest.mark.django_db
def test_matching_id(env):
    """Test matching_id returns transaction ID."""
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
    """Test shred_payment_info removes sensitive data."""
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
def test_api_refund_details(env):
    """Test api_refund_details returns correct data."""
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
    """Test api_refund_details includes error fields when present."""
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
def test_refund_control_render_short(env):
    """Test refund_control_render_short returns correct format."""
    event, order = env

    order.status = Order.STATUS_PAID
    order.save()

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps({"transaction_id": 123456}),
    )

    # With refund ID
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps({"refund_id": 789012}),
    )

    prov = PostFinancePaymentProvider(event)
    result = prov.refund_control_render_short(refund)

    assert result == "PostFinance (789012)"

    # Without refund ID
    refund2 = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        info=json.dumps({}),
    )

    result2 = prov.refund_control_render_short(refund2)
    assert result2 == "PostFinance"


# Session cleanup tests


@pytest.mark.django_db
def test_execute_payment_cleans_session_on_success(env, factory, monkeypatch):
    """Test that session is cleaned up after successful payment."""
    event, order = env

    def get_transaction(transaction_id):
        t = MockedTransaction()
        t.state = TransactionState.COMPLETED
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    # Session should be cleaned up
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_execute_payment_cleans_session_on_api_error(env, factory, monkeypatch):
    """Test that session is cleaned up when API error occurs."""
    event, order = env

    def get_transaction_error(transaction_id):
        raise PostFinanceError("API Error", status_code=500)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction_error(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    # Session should still be cleaned up even after error
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_execute_payment_cleans_session_on_generic_exception(env, factory, monkeypatch):
    """Test that session is cleaned up when generic exception occurs."""
    event, order = env

    def get_transaction_error(transaction_id):
        raise RuntimeError("Unexpected error")

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction_error(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    # Session should still be cleaned up even after error
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_clears_stale_session(env, factory, monkeypatch):
    """Test that checkout_prepare clears any stale transaction ID at start."""
    event, order = env

    created_transaction = MockedTransaction()
    created_transaction.id = 999888

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}  # Stale ID
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    # Should return payment URL
    assert result == "https://checkout.postfinance.ch/pay/999888"
    # Session should have new transaction ID, not the stale one
    assert req.session.get("payment_postfinance_transaction_id") == 999888


@pytest.mark.django_db
def test_checkout_prepare_cleans_session_on_payment_url_failure(env, factory, monkeypatch):
    """Test that session is cleaned when get_payment_page_url fails."""
    event, order = env

    created_transaction = MockedTransaction()
    created_transaction.id = 999888

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: None,  # Simulate failure
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {}
    req.event = event
    req._messages = []  # Mock messages

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    # Should return False
    assert result is False
    # Session should be cleaned up
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_cleans_session_on_api_error(env, factory, monkeypatch):
    """Test that session is cleaned when API error occurs during checkout_prepare."""
    event, order = env

    def create_transaction_error(**kwargs):
        raise PostFinanceError("API Error", status_code=500)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: create_transaction_error(**kwargs),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}  # Pre-existing
    req.event = event
    req._messages = []  # Mock messages

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    # Should return False
    assert result is False
    # Session should be cleaned up
    assert "payment_postfinance_transaction_id" not in req.session


# Additional checkout prepare tests


@pytest.mark.django_db
def test_checkout_prepare_success(env, factory, monkeypatch):
    """Test successful checkout_prepare returns payment URL."""
    event, order = env

    created_transaction = MockedTransaction()
    created_transaction.id = 999888

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    # Should return payment URL
    assert result == "https://checkout.postfinance.ch/pay/999888"
    # Transaction ID should be stored in session
    assert req.session.get("payment_postfinance_transaction_id") == 999888


@pytest.mark.django_db
def test_checkout_prepare_passes_line_items(env, factory, monkeypatch):
    """Test that checkout_prepare passes correct line items to API."""
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(**kwargs):
        captured_kwargs.update(kwargs)
        t = MockedTransaction()
        t.id = 999888
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: capture_create_transaction(**kwargs),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    prov.checkout_prepare(req, cart)

    # Verify line items were passed
    assert "line_items" in captured_kwargs
    assert len(captured_kwargs["line_items"]) == 1  # Fallback to order total


@pytest.mark.django_db
def test_checkout_prepare_passes_allowed_payment_methods(env, factory, monkeypatch):
    """Test that allowed payment methods are passed to API."""
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(**kwargs):
        captured_kwargs.update(kwargs)
        t = MockedTransaction()
        t.id = 999888
        return t

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: capture_create_transaction(**kwargs),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    # Mock the _parse_allowed_payment_methods to return specific values
    monkeypatch.setattr(prov, "_parse_allowed_payment_methods", lambda: [101, 102])

    req = factory.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    prov.checkout_prepare(req, cart)

    assert captured_kwargs["allowed_payment_method_configurations"] == [101, 102]


@pytest.mark.django_db
def test_checkout_prepare_transaction_missing_id(env, factory, monkeypatch):
    """Test checkout_prepare returns False when transaction has no ID."""
    event, order = env

    created_transaction = MockedTransaction()
    created_transaction.id = None  # No ID

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result is False


# API payment details tests


@pytest.mark.django_db
def test_api_payment_details(env):
    """Test api_payment_details returns correct data."""
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
    """Test api_payment_details handles empty info_data."""
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


# Test mode credentials tests


@pytest.fixture
def testmode_env():
    """Create test environment with event in test mode."""
    o = Organizer.objects.create(name="TestOrg", slug="testorg")
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o,
            name="Test Event",
            slug="testevent",
            date_from=now(),
            live=False,
            testmode=True,
            plugins="pretix_postfinance",
        )
        event.settings.set("payment_postfinance_space_id", "12345")
        event.settings.set("payment_postfinance_user_id", "67890")
        event.settings.set("payment_postfinance_auth_key", "live-secret")
        event.settings.set("payment_postfinance__enabled", True)

        order = Order.objects.create(
            code="TESTORDER",
            event=event,
            email="test@test.test",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("10.00"),
            sales_channel=o.sales_channels.get(identifier="web"),
        )
        yield event, order


@pytest.mark.django_db
def test_has_test_credentials_false_when_not_configured(env):
    """Test _has_test_credentials returns False when test credentials not set."""
    event, _ = env
    prov = PostFinancePaymentProvider(event)
    assert prov._has_test_credentials() is False


@pytest.mark.django_db
def test_has_test_credentials_true_when_configured(env):
    """Test _has_test_credentials returns True when all test credentials set."""
    event, _ = env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    prov = PostFinancePaymentProvider(event)
    assert prov._has_test_credentials() is True


@pytest.mark.django_db
def test_has_test_credentials_false_when_partial(env):
    """Test _has_test_credentials returns False when only some test credentials set."""
    event, _ = env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    # user_id and auth_key not set

    prov = PostFinancePaymentProvider(event)
    assert prov._has_test_credentials() is False


@pytest.mark.django_db
def test_get_credentials_returns_live_when_not_testmode(env):
    """Test _get_credentials returns live credentials when event not in test mode."""
    event, _ = env
    event.testmode = False
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    prov = PostFinancePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "12345"
    assert user_id == "67890"
    assert auth_key == "test-secret"  # This is the original live secret


@pytest.mark.django_db
def test_get_credentials_returns_live_when_testmode_but_no_test_creds(testmode_env):
    """Test _get_credentials returns live credentials when in test mode but no test creds."""
    event, _ = testmode_env
    prov = PostFinancePaymentProvider(event)
    space_id, user_id, auth_key = prov._get_credentials()

    assert space_id == "12345"
    assert user_id == "67890"
    assert auth_key == "live-secret"


@pytest.mark.django_db
def test_get_credentials_returns_test_when_testmode_with_test_creds(testmode_env):
    """Test _get_credentials returns test credentials when in test mode with test creds."""
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
    """Test test_mode_message indicates test credentials when configured."""
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
    """Test test_mode_message warns about live credentials when test creds not configured."""
    event, _ = testmode_env
    prov = PostFinancePaymentProvider(event)
    message = prov.test_mode_message

    assert "live credentials" in message.lower()
    assert "no test credentials" in message.lower()


@pytest.mark.django_db
def test_get_client_uses_test_credentials_in_testmode(testmode_env, monkeypatch):
    """Test _get_client uses test credentials when in test mode."""
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
    """Test _get_client uses live credentials when not in test mode."""
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
    assert captured_args["api_secret"] == "test-secret"  # Original live secret


@pytest.mark.django_db
def test_test_connection_indicates_test_mode(testmode_env, monkeypatch):
    """Test test_connection message indicates when using test credentials."""
    event, _ = testmode_env
    event.settings.set("payment_postfinance_test_space_id", "99999")
    event.settings.set("payment_postfinance_test_user_id", "88888")
    event.settings.set("payment_postfinance_test_auth_key", "test-secret")

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: MockedSpace(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "test" in message.lower()
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_indicates_live_mode(env, monkeypatch):
    """Test test_connection message indicates when using live credentials."""
    event, _ = env
    event.testmode = False

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: MockedSpace(),
    )

    prov = PostFinancePaymentProvider(event)
    success, message = prov.test_connection()

    assert success is True
    assert "live" in message.lower()
    assert "Test Space" in message
