from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from django_scopes import scopes_disabled
from postfinancecheckout.models import TransactionState
from pretix.base.models import Order, OrderPayment, OrderRefund, Team, User

from pretix_postfinance.api import PostFinanceError

# ruff: noqa: ARG002


@pytest.fixture
def webhook_env(event, order, organizer):
    user, _ = User.objects.get_or_create(email="admin@localhost", defaults={"password": "admin"})
    team, _ = Team.objects.get_or_create(
        organizer=organizer,
        defaults={"limit_event_permissions": {"event.orders:write": True}},
    )
    team.members.add(user)
    return event, order


@pytest.fixture
def valid_signature(monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
        lambda self, signature_header, content: True,
    )


def get_webhook_payload(entity_id: int, space_id: int = 12345, state: str = "COMPLETED"):
    return {
        "entityId": entity_id,
        "listenerEntityId": 123,
        "spaceId": space_id,
        "state": state,
    }


class TestWebhookTransactionStates:
    @pytest.mark.django_db
    @pytest.mark.parametrize(
        "webhook_state,transaction_state,initial_payment_state,expected_payment_state,expected_order_status",
        [
            pytest.param(
                "FULFILL",
                TransactionState.FULFILL,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_CONFIRMED,
                Order.STATUS_PAID,
                id="fulfill_marks_paid",
            ),
            pytest.param(
                "COMPLETED",
                TransactionState.COMPLETED,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_PENDING,
                Order.STATUS_PENDING,
                id="completed_keeps_pending",
            ),
            pytest.param(
                "FAILED",
                TransactionState.FAILED,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_FAILED,
                Order.STATUS_PENDING,
                id="failed_marks_failed",
            ),
            pytest.param(
                "DECLINE",
                TransactionState.DECLINE,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_FAILED,
                Order.STATUS_PENDING,
                id="decline_marks_failed",
            ),
            pytest.param(
                "VOIDED",
                TransactionState.VOIDED,
                OrderPayment.PAYMENT_STATE_PENDING,
                OrderPayment.PAYMENT_STATE_FAILED,
                Order.STATUS_PENDING,
                id="voided_marks_failed",
            ),
            pytest.param(
                "AUTHORIZED",
                TransactionState.AUTHORIZED,
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
                Order.STATUS_PENDING,
                id="authorized_sets_pending",
            ),
            pytest.param(
                "PENDING",
                TransactionState.PENDING,
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_PENDING,
                Order.STATUS_PENDING,
                id="pending_sets_pending",
            ),
        ],
    )
    def test_transaction_state_handling(
        self,
        webhook_env,
        client,
        monkeypatch,
        valid_signature,
        webhook_state,
        transaction_state,
        initial_payment_state,
        expected_payment_state,
        expected_order_status,
    ):
        event, order = webhook_env
        order.status = Order.STATUS_PENDING
        order.save()

        mock_transaction = MagicMock()
        mock_transaction.state = transaction_state
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
                state=initial_payment_state,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456, state=webhook_state)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == expected_payment_state

        order.refresh_from_db()
        assert order.status == expected_order_status

    @pytest.mark.django_db
    def test_transaction_webhook_persists_info_data(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """
        The transaction webhook must persist the live transaction state into
        info_data. payment_refund_supported() reads info_data["state"], so a
        dropped write here breaks automatic refunds on confirmed payments.
        """
        event, order = webhook_env
        order.status = Order.STATUS_PENDING
        order.save()

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FULFILL
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
                info=json.dumps({"transaction_id": 123456, "state": "AUTHORIZED"}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456, state="FULFILL")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
            assert payment.info_data.get("state") == "FULFILL"
            assert payment.info_data.get("payment_method") == "TWINT"


class TestWebhookSignatureValidation:
    @pytest.mark.django_db
    def test_invalid_signature_returns_401(self, webhook_env, client, monkeypatch):
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
    def test_valid_signature_succeeds(self, webhook_env, client, monkeypatch):
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
            event, order = webhook_env
            order.payments.create(
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


class TestWebhookInvalidPayload:
    @pytest.mark.django_db
    def test_missing_space_id(self, webhook_env, client):
        payload = {"entityId": 123456}

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(payload),
            content_type="application/json",
        )

        assert response.status_code == 400
        assert "spaceid" in response.json().get("error", "").lower()

    @pytest.mark.django_db
    def test_invalid_json(self, webhook_env, client):
        response = client.post(
            "/_postfinance/webhook/",
            "not valid json",
            content_type="application/json",
        )

        assert response.status_code == 400

    @pytest.mark.django_db
    def test_wrong_content_type(self, webhook_env, client):
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456)),
            content_type="text/plain",
        )

        assert response.status_code == 400


class TestWebhookRefund:
    @pytest.mark.django_db
    def test_refund_state_update(self, webhook_env, client, monkeypatch, valid_signature):
        event, order = webhook_env

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
            refund = order.refunds.create(
                provider="postfinance",
                amount=order.total,
                payment=payment,
                state=OrderRefund.REFUND_STATE_TRANSIT,
                info=json.dumps({"refund_id": 789012}),
            )

        refund_payload = get_webhook_payload(789012)

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(refund_payload),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            refund.refresh_from_db()
            assert refund.state == OrderRefund.REFUND_STATE_DONE
            assert refund.info_data.get("state") == "SUCCESSFUL"

    @pytest.mark.django_db
    def test_external_refund_added_to_history(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

        mock_refund = MagicMock()
        mock_refund.state = MagicMock()
        mock_refund.state.value = "SUCCESSFUL"
        mock_refund.amount = 5.00
        mock_refund.created_on = "2026-01-13T11:00:00Z"

        def get_transaction_fail(self, tid):
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
                        "refund_history": [{"refund_id": 999888}],
                    }
                ),
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            )

        refund_payload = get_webhook_payload(999888)

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(refund_payload),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            refund_history = payment.info_data.get("refund_history", [])
            assert len(refund_history) >= 1

    @pytest.mark.django_db
    def test_refund_api_error_stores_error_and_returns_502(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env
        order.status = Order.STATUS_PAID
        order.save()

        def get_refund_fail(refund_id):
            raise PostFinanceError(
                "Refund fetch failed", status_code=500, error_code="SERVER_ERROR"
            )

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

        refund_payload = get_webhook_payload(789012)

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(refund_payload),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 502

        with scopes_disabled():
            refund.refresh_from_db()
            assert refund.info_data.get("error") == "Refund fetch failed"
            assert refund.info_data.get("error_status_code") == 500
            assert refund.info_data.get("error_code") == "SERVER_ERROR"


class TestWebhookErrorHandling:
    @pytest.mark.django_db
    def test_transaction_api_error_returns_502(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

        def get_transaction_fail(tid):
            raise PostFinanceError(
                "API unavailable", status_code=503, error_code="SERVICE_UNAVAILABLE"
            )

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

        assert response.status_code == 502

    @pytest.mark.django_db
    def test_no_client_configured_returns_500(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

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

        payload = get_webhook_payload(777666, space_id=99999)
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(payload),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 500


class TestWebhookIdempotency:
    @pytest.mark.django_db
    def test_already_confirmed_payment_unchanged(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

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
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    @pytest.mark.django_db
    def test_no_matching_payment_returns_200(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(999999)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
