"""
The pages PostFinance sends the customer back to.

pretix's own `pay.complete` and `pay` pages only accept a payment still in
`created`, and a webhook usually moves the payment on before the customer is
back. Customers whose payment was going through were told "The payment for
this order cannot be continued", so the plugin has its own pages, which take
the payment in whatever state it is in.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from django.contrib.messages.storage.cookie import MessageDecoder
from django_scopes import scopes_disabled
from postfinancecheckout.models import TransactionState
from pretix.base.models import Order, OrderPayment

from pretix_postfinance.api import TRANSACTION_ENTITY_ID, PostFinanceError
from pretix_postfinance.payment import RETURN_URL_TAG, PostFinancePaymentProvider

# ruff: noqa: ARG002

TRANSACTION_ID = 4242


@pytest.fixture(autouse=True)
def no_messages():
    """Keep messages: what the customer is told is the point of these pages."""


@pytest.fixture
def get_transaction(monkeypatch, transaction_factory):
    """Answer `get_transaction()` with a transaction in the state given."""
    mock = MagicMock()

    def set_state(state: TransactionState) -> MagicMock:
        mock.side_effect = None
        mock.return_value = transaction_factory(id=TRANSACTION_ID, state=state)
        return mock

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: mock(tid),
    )
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: mock(tid),
    )
    set_state(TransactionState.PROCESSING)
    mock.set_state = set_state
    return mock


@pytest.fixture
def valid_signature(monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.is_webhook_signature_valid",
        lambda self, signature_header, content: True,
    )


def _payment(order, state, info=None):
    with scopes_disabled():
        return order.payments.create(
            provider="postfinance",
            amount=order.total,
            state=state,
            info=json.dumps(
                info
                if info is not None
                else {"pending_transaction_id": TRANSACTION_ID, "space_id": 12345}
            ),
        )


def _url(order, payment, kind="return", secret=None):
    secret = secret or order.tagged_secret(RETURN_URL_TAG)
    return (
        f"/{order.event.organizer.slug}/{order.event.slug}/postfinance/{kind}/"
        f"{order.code}/{secret}/{payment.pk}/"
    )


def _order_url(order, query=""):
    event = order.event
    return f"/{event.organizer.slug}/{event.slug}/order/{order.code}/{order.secret}/{query}"


def _messages(client):
    raw = client.session.get("_messages")
    return [m.message for m in MessageDecoder().decode(raw)] if raw else []


def _refresh(*objs):
    with scopes_disabled():
        for obj in objs:
            obj.refresh_from_db()


@pytest.mark.django_db
def test_transaction_sends_the_customer_back_to_the_plugin(
    env, rf, monkeypatch, transaction_factory
):
    event, order = env
    create = MagicMock(return_value=transaction_factory(id=TRANSACTION_ID))
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: create(**kwargs),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )
    payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED, info={})
    req = rf.get("/")
    req.session = {}

    PostFinancePaymentProvider(event).execute_payment(req, payment)

    kwargs = create.call_args.kwargs
    assert kwargs["success_url"].endswith(_url(order, payment, "return"))
    assert kwargs["failed_url"].endswith(_url(order, payment, "failed"))
    # The order's own secret would open every page of the order.
    assert order.secret not in kwargs["success_url"]
    assert order.secret not in kwargs["failed_url"]


class TestReturn:
    @pytest.mark.django_db
    def test_webhook_got_there_first(self, env, client, get_transaction):
        """The THSRW case: a webhook moved the payment to pending."""
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_PENDING)

        response = client.get(_url(order, payment))

        assert response.status_code == 302
        assert response["Location"] == _order_url(order, "?thanks=yes")
        assert _messages(client) == []
        _refresh(payment)
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
        assert payment.info_data["transaction_id"] == TRANSACTION_ID

    @pytest.mark.django_db
    def test_webhook_already_confirmed(self, env, client, get_transaction):
        event, order = env
        payment = _payment(
            order,
            OrderPayment.PAYMENT_STATE_PENDING,
            info={"transaction_id": TRANSACTION_ID, "space_id": 12345},
        )
        with scopes_disabled():
            payment.confirm()
        get_transaction.side_effect = AssertionError("nothing left to look up")

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order, "?paid=yes")
        assert _messages(client) == []

    @pytest.mark.django_db
    def test_settles_a_payment_the_webhook_has_not_reached(
        self, env, client, get_transaction
    ):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED)
        get_transaction.set_state(TransactionState.FULFILL)

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order, "?paid=yes")
        _refresh(payment, order)
        assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        assert order.status == Order.STATUS_PAID

    @pytest.mark.django_db
    def test_still_processing(self, env, client, get_transaction):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED)

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order, "?thanks=yes")
        _refresh(payment)
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    @pytest.mark.django_db
    def test_failed_while_away(self, env, client, get_transaction):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_PENDING)
        get_transaction.set_state(TransactionState.DECLINE)

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order)
        assert len(_messages(client)) == 1
        _refresh(payment)
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED

    @pytest.mark.django_db
    def test_api_error_leaves_the_payment_alone(self, env, client, get_transaction):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_PENDING)
        get_transaction.side_effect = PostFinanceError("down", status_code=503)

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order)
        assert len(_messages(client)) == 1
        _refresh(payment)
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    @pytest.mark.django_db
    def test_does_not_create_a_transaction(self, env, client, monkeypatch, get_transaction):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED, info={})
        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.create_transaction",
            MagicMock(side_effect=AssertionError("no transaction to be created here")),
        )

        response = client.get(_url(order, payment))

        assert response["Location"] == _order_url(order, "?thanks=yes")
        get_transaction.assert_not_called()


class TestFailed:
    @pytest.mark.django_db
    def test_untouched_payment_can_be_retried(self, env, client, get_transaction):
        """Cancelled before any webhook: back to the retry form, as before."""
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED)

        response = client.get(_url(order, payment, "failed"))

        assert response["Location"] == (
            f"/{event.organizer.slug}/{event.slug}/order/{order.code}/"
            f"{order.secret}/pay/{payment.pk}/"
        )
        get_transaction.assert_not_called()

    @pytest.mark.django_db
    def test_webhook_already_failed_the_payment(self, env, client, get_transaction):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_FAILED)

        response = client.get(_url(order, payment, "failed"))

        assert response["Location"] == _order_url(order)
        assert len(_messages(client)) == 1
        get_transaction.assert_not_called()

    @pytest.mark.django_db
    def test_fails_a_pending_payment_the_webhook_has_not_reached(
        self, env, client, get_transaction
    ):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_PENDING)
        get_transaction.set_state(TransactionState.FAILED)

        response = client.get(_url(order, payment, "failed"))

        assert response["Location"] == _order_url(order)
        assert len(_messages(client)) == 1
        _refresh(payment)
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED


class TestAccess:
    @pytest.mark.django_db
    @pytest.mark.parametrize("kind", ["return", "failed"])
    def test_wrong_secret(self, env, client, get_transaction, kind):
        event, order = env
        payment = _payment(order, OrderPayment.PAYMENT_STATE_PENDING)

        response = client.get(_url(order, payment, kind, secret="0" * 64))

        assert response.status_code == 404
        get_transaction.assert_not_called()

    @pytest.mark.django_db
    def test_other_providers_payment(self, env, client, get_transaction):
        event, order = env
        with scopes_disabled():
            payment = order.payments.create(
                provider="manual",
                amount=order.total,
                state=OrderPayment.PAYMENT_STATE_PENDING,
            )

        response = client.get(_url(order, payment))

        assert response.status_code == 404


@pytest.mark.django_db
def test_customer_returns_between_webhooks(env, client, get_transaction, valid_signature):
    """
    The sequence of order THSRW: CONFIRMED and PROCESSING webhooks, then the
    customer is back, then FULFILL.
    """
    event, order = env
    payment = _payment(order, OrderPayment.PAYMENT_STATE_CREATED)

    def webhook(state):
        get_transaction.set_state(state)
        response = client.post(
            "/_postfinance/webhook/",
            json.dumps(
                {
                    "entityId": TRANSACTION_ID,
                    "listenerEntityId": TRANSACTION_ENTITY_ID,
                    "spaceId": 12345,
                    "state": state.value,
                }
            ),
            content_type="application/json",
            HTTP_X_SIGNATURE="valid-signature",
        )
        assert response.status_code == 200

    webhook(TransactionState.CONFIRMED)
    webhook(TransactionState.PROCESSING)
    _refresh(payment)
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

    response = client.get(_url(order, payment))

    assert response["Location"] == _order_url(order, "?thanks=yes")
    assert _messages(client) == []

    webhook(TransactionState.FULFILL)
    _refresh(payment, order)
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert order.status == Order.STATUS_PAID
