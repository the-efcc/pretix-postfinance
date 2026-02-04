"""
Tests for PostFinance installment payment functionality.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory
from django.utils.timezone import now
from django_scopes import scope
from postfinancecheckout.models import ChargeState, TransactionState
from pretix.base.models import Event, Order, OrderPayment, Organizer

from pretix_postfinance.payment import PostFinancePaymentProvider


class MockedToken:
    """Mock PostFinance Token object."""

    id = 999888
    customer_id = "cus_test123"
    customer_email_address = "test@example.com"


class MockedTransactionWithToken:
    """Mock PostFinance Transaction with token."""

    id = 123456
    state = TransactionState.COMPLETED
    payment_connector_configuration = MagicMock()
    payment_connector_configuration.name = "TWINT"
    created_on = "2026-01-13T10:00:00Z"
    token = MockedToken()


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

        # Enable installments
        event.settings.set("installments_enabled", True)
        event.settings.set("installments_count", 3)

        order = Order.objects.create(
            code="FOOBAR",
            event=event,
            email="dummy@dummy.test",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("300.00"),
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


@pytest.mark.django_db
def test_token_storage_on_first_payment(env, factory, monkeypatch):
    """Test that payment token is stored after successful first installment payment."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan, ScheduledInstallment

    # Create an installment plan
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={},
    )

    # Create scheduled installments
    for i in range(1, 4):
        ScheduledInstallment.objects.create(
            plan=plan,
            due_date=now() + timedelta(days=30 * (i - 1)),
            amount=Decimal("100.00"),
            state=ScheduledInstallment.STATE_PENDING,
        )

    def get_transaction(transaction_id):
        return MockedTransactionWithToken()

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction(tid),
    )

    prov = PostFinancePaymentProvider(event)
    req = factory.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    # Create payment associated with the plan
    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("100.00"),
        installment_plan=plan,
    )

    prov.execute_payment(req, payment)

    # Check that token was stored
    plan.refresh_from_db()
    assert plan.payment_token is not None
    assert plan.payment_token.get("token_id") == 999888
    assert plan.payment_token.get("customer_id") == "cus_test123"
    assert plan.payment_token.get("customer_email") == "test@example.com"

    # Check that token ID is also in payment info
    payment.refresh_from_db()
    assert payment.info_data.get("token_id") == 999888


@pytest.mark.django_db
def test_execute_installment_success(env, factory, monkeypatch):
    """Test successful execution of a scheduled installment using stored token."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan

    # Create an installment plan with stored token
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={
            "token_id": 999888,
            "customer_id": "cus_test123",
            "customer_email": "test@example.com",
        },
    )

    # Mock PostFinance API calls
    created_transaction = MagicMock()
    created_transaction.id = 234567
    created_transaction.state = TransactionState.PENDING

    successful_charge = MagicMock()
    successful_charge.id = 345678
    successful_charge.state = ChargeState.SUCCESSFUL
    successful_charge.failure_reason = None

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: successful_charge,
    )

    prov = PostFinancePaymentProvider(event)

    # Create payment for the installment
    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("100.00"),
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )

    # Execute the installment
    result = prov.execute_installment(payment, plan)

    assert result is True
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert payment.info_data.get("transaction_id") == 234567
    assert payment.info_data.get("token_id") == 999888


@pytest.mark.django_db
def test_execute_installment_no_token(env, factory, monkeypatch):
    """Test installment execution fails when no token is available."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan

    # Create an installment plan WITHOUT a token
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={},
    )

    prov = PostFinancePaymentProvider(event)

    # Create payment for the installment
    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("100.00"),
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )

    # Execute the installment
    result = prov.execute_installment(payment, plan)

    assert result is False


@pytest.mark.django_db
def test_execute_installment_transaction_failed(env, factory, monkeypatch):
    """Test installment execution when PostFinance charge fails."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan

    # Create an installment plan with stored token
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={
            "token_id": 999888,
            "customer_id": "cus_test123",
            "customer_email": "test@example.com",
        },
    )

    # Mock PostFinance API calls with failed charge
    created_transaction = MagicMock()
    created_transaction.id = 234567
    created_transaction.state = TransactionState.PENDING

    failed_charge = MagicMock()
    failed_charge.id = 345678
    failed_charge.state = ChargeState.FAILED
    failed_charge.failure_reason = MagicMock()
    failed_charge.failure_reason.description = "Insufficient funds"

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: failed_charge,
    )

    prov = PostFinancePaymentProvider(event)

    # Create payment for the installment
    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("100.00"),
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )

    # Execute the installment
    result = prov.execute_installment(payment, plan)

    assert result is False
    payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_revoke_payment_token(env, factory, monkeypatch):
    """Test token revocation."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan

    # Create an installment plan with stored token
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={
            "token_id": 999888,
            "customer_id": "cus_test123",
            "customer_email": "test@example.com",
        },
    )

    # Mock delete_token call
    delete_called = []

    def mock_delete_token(token_id):
        delete_called.append(token_id)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.delete_token",
        lambda self, tid: mock_delete_token(tid),
    )

    prov = PostFinancePaymentProvider(event)
    prov.revoke_payment_token(plan)

    # Check that delete was called with the correct token ID
    assert len(delete_called) == 1
    assert delete_called[0] == 999888


@pytest.mark.django_db
def test_revoke_payment_token_no_token(env, factory, monkeypatch):
    """Test token revocation when no token exists (should not raise error)."""
    event, order = env

    # Import here to avoid circular imports
    from pretix.base.models import InstallmentPlan

    # Create an installment plan WITHOUT a token
    plan = InstallmentPlan.objects.create(
        order=order,
        provider="postfinance",
        installments_count=3,
        payment_token={},
    )

    prov = PostFinancePaymentProvider(event)

    # Should not raise any error
    prov.revoke_payment_token(plan)


@pytest.mark.django_db
def test_installments_supported_flag(env):
    """Test that installments_supported flag is set."""
    event, order = env
    prov = PostFinancePaymentProvider(event)
    assert prov.installments_supported is True
