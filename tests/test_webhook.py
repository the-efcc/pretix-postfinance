"""
Tests for the PostFinance webhook handler.

Inspired by pretix's Stripe plugin webhook test suite.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from postfinancecheckout.models import TransactionState
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Organizer, Team, User

from pretix_postfinance.api import PostFinanceError


@pytest.fixture
def valid_signature(monkeypatch):
    """Mock signature validation to always return True."""
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
        lambda self, signature_header, content: True,
    )


@pytest.fixture
def env():
    """Create test environment with organizer, event, order, and user."""
    user = User.objects.create_user("dummy@dummy.dummy", "dummy")
    o = Organizer.objects.create(name="Dummy", slug="dummy")
    event = Event.objects.create(
        organizer=o,
        name="Dummy",
        slug="dummy",
        plugins="pretix_postfinance",
        date_from=now(),
        live=True,
    )
    event.settings.set("payment_postfinance_space_id", "12345")
    event.settings.set("payment_postfinance_user_id", "67890")
    event.settings.set("payment_postfinance_auth_key", "test-secret")

    event.settings.set("payment_postfinance__enabled", True)

    t = Team.objects.create(organizer=event.organizer, can_view_orders=True, can_change_orders=True)
    t.members.add(user)
    t.limit_events.add(event)

    order = Order.objects.create(
        code="FOOBAR",
        event=event,
        email="dummy@dummy.test",
        status=Order.STATUS_PAID,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("13.37"),
        sales_channel=o.sales_channels.get(identifier="web"),
    )
    return event, order


def get_webhook_payload(entity_id: int, space_id: int = 12345, state: str = "COMPLETED"):
    """Create a standard webhook payload."""
    return {
        "entityId": entity_id,
        "listenerEntityId": 123,
        "spaceId": space_id,
        "state": state,
    }


@pytest.mark.django_db
def test_webhook_valid_payload(env, client, monkeypatch, valid_signature):
    """Test webhook with valid payload structure."""
    event, order = env

    # Create a mock transaction
    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.COMPLETED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_webhook_mark_paid(env, client, monkeypatch, valid_signature):
    """Test webhook marking order as paid."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.COMPLETED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_webhook_mark_failed(env, client, monkeypatch, valid_signature):
    """Test webhook marking payment as failed."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.FAILED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456, state="FAILED")),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_webhook_idempotent_already_confirmed(env, client, monkeypatch, valid_signature):
    """Test webhook is idempotent when payment already confirmed."""
    event, order = env

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.COMPLETED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,  # Already confirmed
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    # Payment should still be confirmed (idempotent)
    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_webhook_missing_space_id(env, client):
    """Test webhook with missing spaceId."""
    payload = {"entityId": 123456}  # Missing spaceId

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(payload),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "spaceid" in response.json().get("error", "").lower()


@pytest.mark.django_db
def test_webhook_invalid_json(env, client):
    """Test webhook with invalid JSON payload."""
    response = client.post(
        "/_postfinance/webhook/",
        "not valid json",
        content_type="application/json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_webhook_wrong_content_type(env, client):
    """Test webhook with wrong content type."""
    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="text/plain",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_webhook_no_matching_payment(env, client, monkeypatch, valid_signature):
    """Test webhook with no matching payment record."""
    # No payment created, webhook should return 200 but do nothing

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(999999)),  # Non-existent transaction
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    # Should return 200 to prevent retries
    assert response.status_code == 200


@pytest.mark.django_db
def test_webhook_refund_state_update(env, client, monkeypatch, valid_signature):
    """Test webhook updating refund state on OrderRefund object."""
    event, order = env

    mock_refund = MagicMock()
    mock_refund.state = MagicMock()
    mock_refund.state.value = "SUCCESSFUL"
    mock_refund.amount = 13.37
    mock_refund.created_on = "2026-01-13T11:00:00Z"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_refund",
        lambda self, rid: mock_refund,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        )
        # Create an OrderRefund with the refund_id in its info
        refund = order.refunds.create(
            provider="postfinance",
            amount=order.total,
            payment=payment,
            state=OrderRefund.REFUND_STATE_TRANSIT,
            info=json.dumps({"refund_id": 789012}),
        )

    # Send refund webhook
    refund_payload = get_webhook_payload(789012)  # Refund ID as entityId

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(refund_payload),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    # Check refund was marked as done
    with scopes_disabled():
        refund.refresh_from_db()
        assert refund.state == OrderRefund.REFUND_STATE_DONE
        assert refund.info_data.get("state") == "SUCCESSFUL"


@pytest.mark.django_db
def test_webhook_signature_validation(env, client, monkeypatch):
    """Test webhook signature validation when header is present."""
    # Mock signature validation to return False
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
        lambda self, signature_header, content: False,
    )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="application/json",
        HTTP_X_SIGNATURE="invalid-signature",
    )

    assert response.status_code == 401
    assert "signature" in response.json().get("error", "").lower()


@pytest.mark.django_db
def test_webhook_signature_validation_success(env, client, monkeypatch):
    """Test webhook with valid signature."""
    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.COMPLETED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
        lambda self, signature_header, content: True,
    )
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        event, order = env
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456)),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature-abc123",
    )

    assert response.status_code == 200


@pytest.mark.django_db
def test_webhook_pending_to_created_state(env, client, monkeypatch, valid_signature):
    """Test webhook updating payment from created to pending state."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.PENDING
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "TWINT"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_CREATED,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456, state="PENDING")),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING


@pytest.mark.django_db
def test_webhook_authorized_state_sets_pending(env, client, monkeypatch, valid_signature):
    """Test webhook with AUTHORIZED state sets payment to pending (not confirmed)."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.AUTHORIZED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "Card"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_CREATED,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456, state="AUTHORIZED")),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_webhook_decline_state(env, client, monkeypatch, valid_signature):
    """Test webhook with DECLINE state fails the payment."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.DECLINE
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "Card"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456, state="DECLINE")),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_webhook_voided_state(env, client, monkeypatch, valid_signature):
    """Test webhook with VOIDED state fails the payment."""
    event, order = env
    order.status = Order.STATUS_PENDING
    order.save()

    mock_transaction = MagicMock()
    mock_transaction.state = TransactionState.VOIDED
    mock_transaction.payment_connector_configuration = MagicMock()
    mock_transaction.payment_connector_configuration.name = "Card"

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock_transaction,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(get_webhook_payload(123456, state="VOIDED")),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


@pytest.mark.django_db
def test_webhook_external_refund_added_to_history(env, client, monkeypatch, valid_signature):
    """Test webhook adds external refund to history."""
    event, order = env

    mock_refund = MagicMock()
    mock_refund.state = MagicMock()
    mock_refund.state.value = "SUCCESSFUL"
    mock_refund.amount = 5.00
    mock_refund.created_on = "2026-01-13T11:00:00Z"

    # Mock get_transaction to fail (so it tries refund lookup)
    def get_transaction_fail(self, tid):
        from pretix_postfinance.api import PostFinanceError

        raise PostFinanceError("Not found", status_code=404)

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        get_transaction_fail,
    )
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_refund",
        lambda self, rid: mock_refund,
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps(
                {
                    "transaction_id": 123456,
                    "refund_history": [],  # Empty history
                }
            ),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        )

    # EntityId 999888 is a new refund not in history
    # But it needs to match our payment - let's update info to include it
    with scopes_disabled():
        payment.info = json.dumps(
            {
                "transaction_id": 123456,
                "refund_history": [{"refund_id": 999888}],  # Add refund ID
            }
        )
        payment.save()

    refund_payload = get_webhook_payload(999888)

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(refund_payload),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    assert response.status_code == 200

    # Check refund was updated in history
    with scopes_disabled():
        payment.refresh_from_db()
        refund_history = payment.info_data.get("refund_history", [])
        assert len(refund_history) >= 1


@pytest.mark.django_db
def test_webhook_refund_api_error_stores_error(env, client, monkeypatch, valid_signature):
    """Test that refund webhook API error is stored in refund.info."""
    event, order = env
    order.status = Order.STATUS_PAID
    order.save()

    def get_refund_fail(refund_id):
        raise PostFinanceError("Refund fetch failed", status_code=500, error_code="SERVER_ERROR")

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_refund",
        lambda self, rid: get_refund_fail(rid),
    )

    with scopes_disabled():
        payment = order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 123456}),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        )

        refund = order.refunds.create(
            provider="postfinance",
            amount=order.total,
            payment=payment,
            info=json.dumps({"refund_id": 789012}),
        )

    # Send webhook for this refund
    refund_payload = get_webhook_payload(789012)

    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(refund_payload),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    # API errors now return 502 to trigger PostFinance retry
    assert response.status_code == 502

    # Check error was stored in refund.info
    with scopes_disabled():
        refund.refresh_from_db()
        assert refund.info_data.get("error") == "Refund fetch failed"
        assert refund.info_data.get("error_status_code") == 500
        assert refund.info_data.get("error_code") == "SERVER_ERROR"


@pytest.mark.django_db
def test_webhook_transaction_api_error_returns_502(env, client, monkeypatch, valid_signature):
    """Test that PostFinance API errors for transactions return 502 (retriable)."""
    event, order = env

    def get_transaction_fail(tid):
        raise PostFinanceError("API unavailable", status_code=503, error_code="SERVICE_UNAVAILABLE")

    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: get_transaction_fail(tid),
    )

    with scopes_disabled():
        order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 999888}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    payload = get_webhook_payload(999888)
    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(payload),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    # Should return 502 to trigger PostFinance retry
    assert response.status_code == 502


@pytest.mark.django_db
def test_webhook_no_client_configured_returns_500(env, client, monkeypatch, valid_signature):
    """Test that missing client configuration returns 500 (configuration error)."""
    event, order = env

    # Remove the PostFinance settings
    event.settings.delete("payment_postfinance_space_id")
    event.settings.delete("payment_postfinance_user_id")
    event.settings.delete("payment_postfinance_auth_key")

    with scopes_disabled():
        order.payments.create(
            provider="postfinance",
            amount=order.total,
            info=json.dumps({"transaction_id": 777666}),
            state=OrderPayment.PAYMENT_STATE_PENDING,
        )

    # Use a different space_id that has no configuration
    payload = get_webhook_payload(777666, space_id=99999)
    response = client.post(
        "/_postfinance/webhook/",
        json.dumps(payload),
        content_type="application/json",
        HTTP_X_SIGNATURE="valid-signature",
    )

    # Should return 500 for configuration error
    assert response.status_code == 500
