from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from postfinancecheckout.models import TransactionState
from pretix.base.models import Event, Order, OrderPayment, OrderRefund, Team, User

from pretix_postfinance.api import REFUND_ENTITY_ID, PostFinanceError

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
    def test_unverifiable_signature_returns_401(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """With no credentials for the webhook's space anywhere, the signature
        cannot be checked and the payload must not be acted upon."""
        event, order = webhook_env

        event.settings.delete("payment_postfinance_space_id")
        event.settings.delete("payment_postfinance_user_id")
        event.settings.delete("payment_postfinance_auth_key")

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: pytest.fail("must not fetch the transaction"),
        )

        with scopes_disabled():
            payment = order.payments.create(
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

        assert response.status_code == 401

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    @pytest.mark.django_db
    def test_no_client_for_the_payments_event_returns_500(
        self, webhook_env, client, monkeypatch, valid_signature, organizer
    ):
        """The signature validates against another event configured with the
        space, but the payment's own event cannot serve it — a configuration
        error PostFinance should retry."""
        event, order = webhook_env

        with scopes_disabled():
            other_event = Event.objects.create(
                organizer=organizer,
                name="Other",
                slug="other",
                date_from=now(),
                live=True,
                plugins="pretix_postfinance",
            )
        other_event.settings.set("payment_postfinance_space_id", "99999")
        other_event.settings.set("payment_postfinance_user_id", "88888")
        other_event.settings.set("payment_postfinance_auth_key", "other-secret")

        with scopes_disabled():
            # Recorded in a space its own event is not configured with
            order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"transaction_id": 777666, "space_id": 99999}),
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
    def test_already_confirmed_payment_still_offers_its_token(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """
        The token store runs ahead of the already-settled check.

        A payment confirmed by another route — an organizer marking it paid —
        leaves its installment plan with no token, and returning before the
        store left the plan permanently unchargeable. Asserted on the call
        rather than on a plan so this also runs on an upstream pretix, which
        has no installments at all.
        """
        event, order = webhook_env

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FULFILL
        mock_transaction.payment_connector_configuration = MagicMock()
        mock_transaction.payment_connector_configuration.name = "Visa"
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: mock_transaction,
        )

        offered: list[int] = []
        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinancePaymentProvider.store_installment_token",
            lambda self, payment, transaction: offered.append(payment.pk),
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
        assert offered == [payment.pk]

    @pytest.mark.django_db
    def test_a_failed_transaction_offers_no_token(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """A declined card is not worth storing for later charges."""
        event, order = webhook_env

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FAILED
        mock_transaction.payment_connector_configuration = MagicMock()
        mock_transaction.payment_connector_configuration.name = "Visa"
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: mock_transaction,
        )

        offered: list[int] = []
        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinancePaymentProvider.store_installment_token",
            lambda self, payment, transaction: offered.append(payment.pk),
        )

        with scopes_disabled():
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
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
        assert offered == []

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


class TestWebhookSpaces:
    @pytest.mark.django_db
    def test_prod_provider_payment_is_processed(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """Payments made through the production space provider in test mode
        must be found and confirmed by the webhook handler."""
        event, order = webhook_env
        order.testmode = True
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
                provider="postfinance_prod",
                amount=order.total,
                info=json.dumps({"transaction_id": 424242}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(424242)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    @pytest.mark.django_db
    def test_test_space_webhook_uses_test_credentials(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """A webhook from the configured test space must be processed with
        the test credentials instead of being rejected as a space mismatch."""
        event, order = webhook_env
        event.settings.set("payment_postfinance_test_space_id", "99999")
        event.settings.set("payment_postfinance_test_user_id", "88888")
        event.settings.set("payment_postfinance_test_auth_key", "test-secret")
        order.testmode = True
        order.status = Order.STATUS_PENDING
        order.save()

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FULFILL
        mock_transaction.payment_connector_configuration = MagicMock()
        mock_transaction.payment_connector_configuration.name = "TWINT"

        used_spaces = []

        def get_transaction(self, tid):
            used_spaces.append(self.space_id)
            return mock_transaction

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            get_transaction,
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"transaction_id": 434343}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(434343, space_id=99999)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
        assert used_spaces == [99999]

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    @pytest.mark.django_db
    def test_unknown_space_is_rejected(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """A webhook from a space no event is configured with cannot have its
        signature checked, so it must be rejected instead of processed."""
        event, order = webhook_env

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: pytest.fail("must not fetch the transaction"),
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"transaction_id": 454545}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(454545, space_id=31337)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 401

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    @pytest.mark.django_db
    def test_test_space_webhook_does_not_touch_live_payment(
        self, webhook_env, client, monkeypatch, valid_signature, organizer
    ):
        """Transaction IDs are only unique within a space. A webhook from the
        test space must not be applied to a live space payment that happens to
        carry the same transaction ID."""
        event, live_order = webhook_env
        event.settings.set("payment_postfinance_test_space_id", "99999")
        event.settings.set("payment_postfinance_test_user_id", "88888")
        event.settings.set("payment_postfinance_test_auth_key", "test-secret")

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FULFILL
        mock_transaction.payment_connector_configuration = MagicMock()
        mock_transaction.payment_connector_configuration.name = "TWINT"

        used_spaces = []

        def get_transaction(self, tid):
            used_spaces.append(self.space_id)
            return mock_transaction

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction", get_transaction
        )

        with scopes_disabled():
            live_payment = live_order.payments.create(
                provider="postfinance",
                amount=live_order.total,
                info=json.dumps({"transaction_id": 7, "space_id": 12345}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )
            test_order = Order.objects.create(
                code="TESTMD",
                event=event,
                email="test@test.test",
                status=Order.STATUS_PENDING,
                datetime=now(),
                expires=now() + timedelta(days=10),
                total=live_order.total,
                testmode=True,
                sales_channel=organizer.sales_channels.get(identifier="web"),
            )
            test_payment = test_order.payments.create(
                provider="postfinance",
                amount=test_order.total,
                info=json.dumps({"transaction_id": 7, "space_id": 99999}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(7, space_id=99999)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
        assert used_spaces == [99999]

        with scopes_disabled():
            live_payment.refresh_from_db()
            test_payment.refresh_from_db()
            assert live_payment.state == OrderPayment.PAYMENT_STATE_PENDING
            assert test_payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    @pytest.mark.django_db
    def test_space_is_derived_for_payments_without_a_recorded_space(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """Payments created before the space was recorded on them must still
        be matched, using the space their order's test mode implies."""
        event, order = webhook_env
        event.settings.set("payment_postfinance_test_space_id", "99999")
        event.settings.set("payment_postfinance_test_user_id", "88888")
        event.settings.set("payment_postfinance_test_auth_key", "test-secret")
        order.testmode = True
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
                info=json.dumps({"transaction_id": 474747}),
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(474747, space_id=99999)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
            assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    @pytest.mark.django_db
    def test_refund_webhook_is_not_swallowed_by_a_colliding_transaction_id(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """Transaction and refund IDs are separate sequences and can collide.
        A refund webhook must reach the refund handler even if a payment
        carries the same ID as a transaction ID."""
        event, order = webhook_env
        order.status = Order.STATUS_PAID
        order.save()

        mock_refund = MagicMock()
        mock_refund.state = MagicMock()
        mock_refund.state.value = "SUCCESSFUL"

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_refund",
            lambda self, rid: mock_refund,
        )
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: pytest.fail("refund webhook must not be handled as transaction"),
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"transaction_id": 4242, "space_id": 12345}),
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            )
            refund = order.refunds.create(
                provider="postfinance",
                payment=payment,
                amount=order.total,
                info=json.dumps({"refund_id": 4242, "space_id": 12345}),
                state=OrderRefund.REFUND_STATE_TRANSIT,
            )

        payload = get_webhook_payload(4242)
        payload["listenerEntityId"] = REFUND_ENTITY_ID
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(payload),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            refund.refresh_from_db()
            assert refund.state == OrderRefund.REFUND_STATE_DONE

    @pytest.mark.django_db
    def test_prod_provider_refund_webhook_is_processed(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """Refunds of production space payments made in test mode must be
        found and completed by the webhook handler."""
        event, order = webhook_env
        order.testmode = True
        order.status = Order.STATUS_PAID
        order.save()

        mock_refund = MagicMock()
        mock_refund.state = MagicMock()
        mock_refund.state.value = "SUCCESSFUL"

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_refund",
            lambda self, rid: mock_refund,
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance_prod",
                amount=order.total,
                info=json.dumps({"transaction_id": 464646}),
                state=OrderPayment.PAYMENT_STATE_CONFIRMED,
            )
            refund = order.refunds.create(
                provider="postfinance_prod",
                payment=payment,
                amount=order.total,
                info=json.dumps({"refund_id": 565656}),
                state=OrderRefund.REFUND_STATE_TRANSIT,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(565656)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            refund.refresh_from_db()
            assert refund.state == OrderRefund.REFUND_STATE_DONE


class TestWebhookPendingTransaction:
    """
    A transaction is recorded under ``pending_transaction_id`` from the moment
    the customer is sent to the payment page, and only promoted to
    ``transaction_id`` when they come back. A webhook that arrives before that
    has to find the payment anyway — otherwise a customer who closes the tab
    after paying leaves the order unpaid forever, and an installment plan
    never gets the token its later charges depend on.
    """

    @pytest.mark.django_db
    def test_pending_transaction_id_is_matched(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FULFILL
        mock_transaction.payment_connector_configuration = None
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: mock_transaction,
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"pending_transaction_id": 778899, "space_id": 12345}),
                state=OrderPayment.PAYMENT_STATE_CREATED,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(778899, state="FULFILL")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
        order.refresh_from_db()

        assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        assert order.status == Order.STATUS_PAID

    @pytest.mark.django_db
    def test_pending_key_is_promoted_not_duplicated(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """The provisional key is dropped once the final one is written."""
        event, order = webhook_env

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.AUTHORIZED
        mock_transaction.payment_connector_configuration = None
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: mock_transaction,
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"pending_transaction_id": 778899, "space_id": 12345}),
                state=OrderPayment.PAYMENT_STATE_CREATED,
            )

        client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(778899, state="AUTHORIZED")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        with scopes_disabled():
            payment.refresh_from_db()

        assert payment.info_data["transaction_id"] == 778899
        assert "pending_transaction_id" not in payment.info_data

    @pytest.mark.django_db
    def test_pending_transaction_from_another_space_is_ignored(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """The space still has to match — IDs are only unique within one."""
        event, order = webhook_env

        called = []
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: called.append(tid),
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"pending_transaction_id": 778899, "space_id": 99999}),
                state=OrderPayment.PAYMENT_STATE_CREATED,
            )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(778899, space_id=12345, state="FULFILL")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
        assert called == []

        with scopes_disabled():
            payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


class TestWebhookFailurePreservesInfo:
    """
    ``OrderPayment.fail()`` assigns whatever it is given to ``info_data``
    instead of merging it. Failing a payment must therefore pass the existing
    data back in, or the transaction reference, the space and the FX snapshot
    are destroyed — which breaks the dashboard link, ``matching_id()`` and any
    later webhook for the same transaction.
    """

    @pytest.mark.django_db
    def test_failed_webhook_keeps_transaction_reference(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        event, order = webhook_env

        mock_transaction = MagicMock()
        mock_transaction.state = TransactionState.FAILED
        mock_transaction.payment_connector_configuration = None
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction",
            lambda self, tid: mock_transaction,
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps(
                    {
                        "transaction_id": 123456,
                        "space_id": 12345,
                        "state": "PROCESSING",
                        "fx_rate": "1.08",
                        "fx_base_currency": "EUR",
                        "charged_currency": "CHF",
                        "charged_amount": "14.44",
                    }
                ),
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
        assert payment.info_data["state"] == "FAILED"
        assert payment.info_data["transaction_id"] == 123456
        assert payment.info_data["space_id"] == 12345
        assert payment.info_data["charged_currency"] == "CHF"
        assert payment.info_data["charged_amount"] == "14.44"
        assert payment.info_data["fx_rate"] == "1.08"

    @pytest.mark.django_db
    def test_failed_payment_is_still_findable_by_a_later_webhook(
        self, webhook_env, client, monkeypatch, valid_signature
    ):
        """Keeping transaction_id is what lets _find_entity match again."""
        event, order = webhook_env

        state = {"value": TransactionState.FAILED}
        mock_transaction = MagicMock()
        mock_transaction.payment_connector_configuration = None

        def get_transaction(self, tid):
            mock_transaction.state = state["value"]
            return mock_transaction

        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.get_transaction", get_transaction
        )

        with scopes_disabled():
            payment = order.payments.create(
                provider="postfinance",
                amount=order.total,
                info=json.dumps({"pending_transaction_id": 123456, "space_id": 12345}),
                state=OrderPayment.PAYMENT_STATE_CREATED,
            )

        client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456, state="FAILED")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        state["value"] = TransactionState.VOIDED
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(123456, state="VOIDED")),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200

        with scopes_disabled():
            payment.refresh_from_db()
        assert payment.info_data["transaction_id"] == 123456



class TestWebhookSpaceLookup:
    """
    Signature validation needs credentials for the space a webhook came from,
    and finding them must not depend on how many events an installation has.
    """

    @pytest.mark.django_db
    def test_organizer_level_settings_are_found(self, organizer, event):
        """
        Hierarkey resolves an unset event setting from the organizer, so an
        event configured at organizer level has no settings row of its own.
        Before, only the unordered fallback scan could find it — which on an
        installation with more than a hundred live events is a coin toss.
        """
        from pretix_postfinance.views import _events_for_space

        for key in (
            "payment_postfinance_space_id",
            "payment_postfinance_user_id",
            "payment_postfinance_auth_key",
        ):
            event.settings.delete(key)
        organizer.settings.set("payment_postfinance_space_id", "55555")
        organizer.settings.set("payment_postfinance_user_id", "67890")
        organizer.settings.set("payment_postfinance_auth_key", "org-secret")

        found = list(_events_for_space(55555))

        assert event.pk in [e.pk for e in found]
        # And it comes from the indexed lookup, not the bounded scan.
        assert found[0].pk == event.pk

    @pytest.mark.django_db
    def test_event_level_settings_come_first(self, event):
        from pretix_postfinance.views import _events_for_space

        found = list(_events_for_space(12345))

        assert found[0].pk == event.pk

    @pytest.mark.django_db
    def test_no_event_is_yielded_twice(self, organizer, event):
        """An event matching at both levels must not be visited repeatedly."""
        from pretix_postfinance.views import _events_for_space

        organizer.settings.set("payment_postfinance_space_id", "12345")

        found = [e.pk for e in _events_for_space(12345)]

        assert found.count(event.pk) == 1

    @pytest.mark.django_db
    def test_the_fallback_scan_is_ordered(self, event):
        """
        Which events the last-resort scan covers has to be reproducible, or a
        space configured somewhere the indexed queries cannot see is found
        only intermittently.
        """
        from pretix_postfinance.views import _events_for_space

        first = [e.pk for e in _events_for_space(77777)]
        second = [e.pk for e in _events_for_space(77777)]

        assert first == second
        assert first == sorted(first)

    @pytest.mark.django_db
    def test_organizer_credentials_validate_the_signature(
        self, organizer, event, client, monkeypatch
    ):
        """The end-to-end point of the lookup: the webhook is accepted."""
        for key in (
            "payment_postfinance_space_id",
            "payment_postfinance_user_id",
            "payment_postfinance_auth_key",
        ):
            event.settings.delete(key)
        organizer.settings.set("payment_postfinance_space_id", "55555")
        organizer.settings.set("payment_postfinance_user_id", "67890")
        organizer.settings.set("payment_postfinance_auth_key", "org-secret")

        checked = []
        monkeypatch.setattr(
            "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
            lambda self, signature_header, content: checked.append(self.space_id) or True,
        )

        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(get_webhook_payload(313131, space_id=55555)),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )

        assert response.status_code == 200
        assert checked == [55555]
