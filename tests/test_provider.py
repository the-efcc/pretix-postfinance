from __future__ import annotations

import json
from decimal import Decimal

import pytest
from postfinancecheckout.models import TransactionState
from pretix.base.models import Order, OrderPayment, OrderRefund
from pretix.base.payment import PaymentException

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import PostFinancePaymentProvider


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state,expected_order_status,expected_payment_state",
    [
        (TransactionState.COMPLETED, Order.STATUS_PAID, None),
        (TransactionState.AUTHORIZED, Order.STATUS_PENDING, None),
        (TransactionState.FAILED, Order.STATUS_PENDING, OrderPayment.PAYMENT_STATE_FAILED),
        (TransactionState.DECLINE, Order.STATUS_PENDING, OrderPayment.PAYMENT_STATE_FAILED),
    ],
    ids=["completed", "authorized", "failed", "declined"],
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
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == expected_order_status

    if expected_payment_state is not None:
        payment.refresh_from_db()
        assert payment.state == expected_payment_state


@pytest.mark.django_db
def test_execute_payment_api_error(env, rf, monkeypatch):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: (_ for _ in ()).throw(PostFinanceError("API Error", status_code=500)),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_execute_payment_no_transaction_id(env, rf):
    event, order = env

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    req.session = {}

    payment = order.payments.create(provider="postfinance", amount=order.total)
    result = prov.execute_payment(req, payment)

    assert result is None
    payment.refresh_from_db()
    assert payment.info_data.get("error") == "No transaction ID in session"


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
        ({}, False),
    ],
    ids=["with_transaction_id", "without_transaction_id"],
)
def test_payment_is_valid_session(env, rf, session, expected):
    event, _ = env

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = session
    assert prov.payment_is_valid_session(req) is expected


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
    req.session = {"payment_postfinance_transaction_id": 123456}

    payment = order.payments.create(provider="postfinance", amount=order.total)

    if exception:
        with pytest.raises(PaymentException):
            prov.execute_payment(req, payment)
    else:
        prov.execute_payment(req, payment)

    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_clears_stale_session(env, rf, monkeypatch, transaction_factory):
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
    req = rf.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert req.session.get("payment_postfinance_transaction_id") == 999888


@pytest.mark.django_db
def test_checkout_prepare_cleans_session_on_payment_url_failure(
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
    req = rf.post("/")
    req.session = {}
    req.event = event
    req._messages = []

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result is False
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_cleans_session_on_api_error(env, rf, monkeypatch):
    event, order = env

    def raise_api_error(**kwargs):
        raise PostFinanceError("API Error", status_code=500)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: raise_api_error(**kwargs),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    req.session = {"payment_postfinance_transaction_id": 123456}
    req.event = event
    req._messages = []

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result is False
    assert "payment_postfinance_transaction_id" not in req.session


@pytest.mark.django_db
def test_checkout_prepare_success(env, rf, monkeypatch, transaction_factory):
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
    req = rf.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert req.session.get("payment_postfinance_transaction_id") == 999888


@pytest.mark.django_db
def test_checkout_prepare_passes_line_items(env, rf, monkeypatch, transaction_factory):
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(**kwargs):
        captured_kwargs.update(kwargs)
        return transaction_factory(id=999888)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: capture_create_transaction(**kwargs),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    prov.checkout_prepare(req, cart)

    assert "line_items" in captured_kwargs
    assert len(captured_kwargs["line_items"]) == 1


@pytest.mark.django_db
def test_checkout_prepare_passes_allowed_payment_methods(env, rf, monkeypatch, transaction_factory):
    event, order = env

    captured_kwargs = {}

    def capture_create_transaction(**kwargs):
        captured_kwargs.update(kwargs)
        return transaction_factory(id=999888)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: capture_create_transaction(**kwargs),
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: f"https://checkout.postfinance.ch/pay/{tid}",
    )

    prov = PostFinancePaymentProvider(event)
    monkeypatch.setattr(prov, "_parse_allowed_payment_methods", lambda: [101, 102])

    req = rf.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    prov.checkout_prepare(req, cart)

    assert captured_kwargs["allowed_payment_method_configurations"] == [101, 102]


@pytest.mark.django_db
def test_checkout_prepare_transaction_missing_id(env, rf, monkeypatch, transaction_factory):
    event, order = env

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: transaction_factory(id=None),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.post("/")
    req.session = {}
    req.event = event

    cart = {"total": order.total, "positions": [], "fees": []}
    result = prov.checkout_prepare(req, cart)

    assert result is False


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
