from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ValidationError
from postfinancecheckout.models import TransactionState
from pretix.base.models import Order, OrderPayment, OrderRefund
from pretix.base.payment import PaymentException

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import ERROR_STATUS_MESSAGES, PostFinancePaymentProvider
from pretix_postfinance.views import _process_transaction_webhook

PAY_ALT_SESSION_KEY = "payment_postfinance_pay_alt"


@pytest.fixture
def alt_event(event):
    event.settings.set("payment_postfinance_alt_currency", "CHF")
    event.settings.set("payment_postfinance_alt_currency_rate", "0.93")
    return event


@pytest.fixture
def provider(alt_event):
    return PostFinancePaymentProvider(alt_event)


# Configuration


@pytest.mark.django_db
def test_alt_currency_config_parsed(provider):
    assert provider.alt_currency_config == ("CHF", Decimal("0.93"))


@pytest.mark.django_db
def test_alt_currency_config_none_without_settings(event):
    assert PostFinancePaymentProvider(event).alt_currency_config is None


@pytest.mark.django_db
def test_alt_currency_config_none_when_same_as_event_currency(alt_event):
    alt_event.currency = "CHF"
    alt_event.save()
    assert PostFinancePaymentProvider(alt_event).alt_currency_config is None


@pytest.mark.django_db
def test_alt_currency_config_none_without_rate(event):
    event.settings.set("payment_postfinance_alt_currency", "CHF")
    assert PostFinancePaymentProvider(event).alt_currency_config is None


@pytest.mark.django_db
def test_alt_currency_config_none_with_unparseable_rate(event, caplog):
    event.settings.set("payment_postfinance_alt_currency", "CHF")
    event.settings.set("payment_postfinance_alt_currency_rate", "not-a-number")

    assert PostFinancePaymentProvider(event).alt_currency_config is None
    assert "unparseable PostFinance exchange rate" in caplog.text


@pytest.mark.django_db
def test_settings_form_has_alt_currency_fields(alt_event, monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )
    fields = PostFinancePaymentProvider(alt_event).settings_form_fields
    assert "alt_currency" in fields
    assert "alt_currency_rate" in fields
    # The event's own currency is not offered as an alternative
    assert "EUR" not in dict(fields["alt_currency"].choices)
    assert "CHF" in dict(fields["alt_currency"].choices)


@pytest.mark.django_db
def test_settings_form_clean_requires_rate_with_currency(provider):
    with pytest.raises(ValidationError):
        provider.settings_form_clean({"alt_currency": "CHF", "alt_currency_rate": None})

    cleaned = {"alt_currency": "CHF", "alt_currency_rate": Decimal("0.93")}
    assert provider.settings_form_clean(dict(cleaned)) == cleaned


@pytest.mark.django_db
def test_conversion_rounds_half_up(provider):
    assert provider._convert(Decimal("13.37"), Decimal("0.93")) == Decimal("12.43")
    assert provider._convert(Decimal("10.00"), Decimal("0.955")) == Decimal("9.55")
    # 1.005 rounds up, not to even
    assert provider._convert(Decimal("1.00"), Decimal("1.005")) == Decimal("1.01")


# Checkout form


@pytest.mark.django_db
def test_payment_form_has_checkbox_when_configured(provider):
    assert "pay_alt" in provider.payment_form_fields


@pytest.mark.django_db
def test_payment_form_has_no_checkbox_without_config(event):
    assert "pay_alt" not in PostFinancePaymentProvider(event).payment_form_fields


@pytest.mark.django_db
def test_payment_form_render_shows_converted_amount(provider, rf):
    req = rf.get("/")
    req.event = provider.event
    req.session = {}

    html = provider.payment_form_render(req, Decimal("13.37"))

    assert "Pay in CHF" in html
    assert "12.43" in html
    assert "0.93" in html


@pytest.mark.django_db
def test_payment_form_checkbox_is_aligned_with_the_panel(provider, rf):
    """The checkbox has to line up with the rest of the payment panel.

    pretix renders the panel body as .form-horizontal, so the field's
    .form-group is a row with -15px side margins. A full width column puts
    that gutter back; an empty col-md-3 label column (pretix's own template)
    would instead indent the checkbox into the middle of the panel.
    """
    req = rf.get("/")
    req.event = provider.event
    req.session = {}

    html = provider.payment_form_render(req, Decimal("13.37"))

    assert "form-group col-md-12" in html
    assert "col-md-3" not in html
    assert "col-md-9" not in html
    assert 'class="checkbox"' in html


@pytest.mark.django_db
def test_whole_rate_is_not_rendered_in_scientific_notation(alt_event, rf):
    # Decimal("160").normalize() is 1.6E+2, which must not reach customers.
    alt_event.settings.set("payment_postfinance_alt_currency", "JPY")
    alt_event.settings.set("payment_postfinance_alt_currency_rate", "160.000000")
    provider = PostFinancePaymentProvider(alt_event)
    req = rf.get("/")
    req.event = alt_event
    req.session = {PAY_ALT_SESSION_KEY: True}

    form_html = provider.payment_form_render(req, Decimal("13.37"))
    confirm_html = provider.checkout_confirm_render(req)

    assert "1 EUR = 160 JPY" in form_html
    assert "1 EUR = 160 JPY" in confirm_html
    assert "E+" not in form_html
    assert "E+" not in confirm_html


@pytest.mark.django_db
def test_checkout_confirm_render_mentions_rate_when_opted_in(provider, rf):
    req = rf.get("/")
    req.event = provider.event

    req.session = {PAY_ALT_SESSION_KEY: True}
    assert "0.93" in provider.checkout_confirm_render(req)

    req.session = {}
    assert "0.93" not in provider.checkout_confirm_render(req)


# Transaction creation


def _mock_transaction_creation(monkeypatch, captured):
    def fake_create_transaction(self, **kwargs):
        captured.update(kwargs)
        transaction = MagicMock()
        transaction.id = 999888
        return transaction

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        fake_create_transaction,
    )
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_page_url",
        lambda self, tid: "https://checkout.postfinance.ch/pay/999888",
    )


@pytest.mark.django_db
def test_opted_in_payment_charged_in_alt_currency(alt_event, order, rf, monkeypatch):
    captured = {}
    _mock_transaction_creation(monkeypatch, captured)

    provider = PostFinancePaymentProvider(alt_event)
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,  # 13.37 EUR
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )
    req = rf.post("/")
    req.session = {PAY_ALT_SESSION_KEY: True}

    result = provider.execute_payment(req, payment)

    assert result == "https://checkout.postfinance.ch/pay/999888"
    assert captured["currency"] == "CHF"
    line_items = captured["line_items"]
    assert len(line_items) == 1
    assert line_items[0].amount_including_tax == pytest.approx(12.43)  # 13.37 * 0.93

    payment.refresh_from_db()
    assert payment.info_data["pending_transaction_id"] == 999888
    assert payment.info_data["fx_rate"] == "0.93"
    assert payment.info_data["fx_base_currency"] == "EUR"
    assert payment.info_data["charged_currency"] == "CHF"
    assert payment.info_data["charged_amount"] == "12.43"


@pytest.mark.django_db
def test_opted_out_payment_charged_in_event_currency(alt_event, order, rf, monkeypatch):
    captured = {}
    _mock_transaction_creation(monkeypatch, captured)

    provider = PostFinancePaymentProvider(alt_event)
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CREATED,
    )
    req = rf.post("/")
    req.session = {}

    provider.execute_payment(req, payment)

    assert captured["currency"] == "EUR"
    payment.refresh_from_db()
    assert "fx_rate" not in payment.info_data
    assert "charged_currency" not in payment.info_data


@pytest.mark.django_db
def test_retry_without_checkbox_clears_stale_fx_snapshot(alt_event, order, rf, monkeypatch):
    captured = {}
    _mock_transaction_creation(monkeypatch, captured)

    provider = PostFinancePaymentProvider(alt_event)
    # Payment from an earlier attempt where the customer had opted into CHF
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CREATED,
        info=json.dumps(
            {
                "fx_rate": "0.93",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "12.43",
            }
        ),
    )
    req = rf.post("/")
    req.session = {}

    provider.execute_payment(req, payment)

    assert captured["currency"] == "EUR"
    payment.refresh_from_db()
    assert "fx_rate" not in payment.info_data
    assert "charged_currency" not in payment.info_data


@pytest.mark.django_db
def test_execute_payment_confirm_preserves_fx_snapshot(
    alt_event, order, rf, monkeypatch, transaction_factory
):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(state=TransactionState.FULFILL),
    )

    provider = PostFinancePaymentProvider(alt_event)
    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CREATED,
        info=json.dumps(
            {
                "pending_transaction_id": 123456,
                "fx_rate": "0.93",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "12.43",
            }
        ),
    )
    req = rf.post("/")
    req.session = {}

    provider.execute_payment(req, payment)

    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    assert payment.info_data["transaction_id"] == 123456
    assert "pending_transaction_id" not in payment.info_data
    # The FX snapshot must survive confirmation for later refunds
    assert payment.info_data["fx_rate"] == "0.93"
    assert payment.info_data["charged_amount"] == "12.43"


# Refunds


def _paid_payment(order, fx=True, fx_rate="0.93"):
    info = {
        "transaction_id": 123456,
        "state": TransactionState.COMPLETED.value,
    }
    if fx:
        info.update(
            {
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "12.43",
            }
        )
        if fx_rate is not None:
            info["fx_rate"] = fx_rate
    return order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        info=json.dumps(info),
    )


def _mock_refund(monkeypatch, captured, refund_factory):
    def fake_refund_transaction(self, **kwargs):
        captured.update(kwargs)
        return refund_factory()

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.refund_transaction",
        fake_refund_transaction,
    )


@pytest.mark.django_db
def test_refund_of_event_currency_payment_unchanged(
    alt_event, order, monkeypatch, refund_factory
):
    captured = {}
    _mock_refund(monkeypatch, captured, refund_factory)

    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order, fx=False)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    provider.execute_refund(refund)

    assert captured["amount"] == Decimal("5.00")


@pytest.mark.django_db
def test_full_refund_of_alt_currency_payment_sends_charged_amount(
    alt_event, order, monkeypatch, refund_factory
):
    captured = {}
    _mock_refund(monkeypatch, captured, refund_factory)

    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
    )

    provider.execute_refund(refund)

    # PostFinance rejects a refund with neither an amount nor reductions,
    # so full refunds send the exact charge recorded at payment time.
    assert captured["amount"] == Decimal("12.43")
    refund.refresh_from_db()
    assert refund.state == OrderRefund.REFUND_STATE_DONE


@pytest.mark.django_db
def test_partial_refund_of_alt_currency_payment_is_converted(
    alt_event, order, monkeypatch, refund_factory
):
    captured = {}
    _mock_refund(monkeypatch, captured, refund_factory)

    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    provider.execute_refund(refund)

    assert captured["amount"] == Decimal("4.65")  # 5.00 EUR * 0.93


@pytest.mark.django_db
def test_final_partial_refund_draws_down_the_remaining_charge(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 1, "amount": 4.65}),
    )
    remainder = order.refunds.create(
        provider="postfinance",
        amount=order.total - Decimal("5.00"),
        payment=payment,
    )

    # 12.43 CHF charged minus the 4.65 CHF PostFinance already refunded
    assert provider._refund_transaction_amount(remainder) == Decimal("7.78")


@pytest.mark.django_db
def test_remainder_ignores_rounding_drift_of_the_converted_parts(alt_event, order):
    """The remainder is what is left of the charge, not a re-conversion.

    10.00 EUR at 0.925 is charged as 9.25 CHF, but each 5.00 EUR half
    converts to 4.63 CHF (4.6250 rounded half up) — 9.26 CHF together, a
    cent more than was ever charged. The second refund has to return the
    4.62 CHF actually left, or PostFinance rejects it.
    """
    provider = PostFinancePaymentProvider(alt_event)
    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("10.00"),
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "state": TransactionState.COMPLETED.value,
                "fx_rate": "0.925",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "9.25",
            }
        ),
    )
    order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 1, "amount": 4.63}),
    )
    remainder = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(remainder) == Decimal("4.62")


@pytest.mark.django_db
def test_refund_of_fully_refunded_charge_raises(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 1, "amount": 12.43}),
    )
    extra = order.refunds.create(
        provider="postfinance",
        amount=Decimal("1.00"),
        payment=payment,
    )

    with pytest.raises(PaymentException):
        provider._refund_transaction_amount(extra)


@pytest.mark.django_db
def test_refund_settled_by_other_means_is_not_a_postfinance_remainder(alt_event, order):
    # A manual refund does not draw on the PostFinance transaction, so the
    # PostFinance refund that follows is still a partial one and must be
    # converted instead of refunding the whole outstanding charge.
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    order.refunds.create(
        provider="manual",
        amount=order.total - Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
    )
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(refund) == Decimal("4.65")


@pytest.mark.django_db
def test_refund_of_production_space_payment_counts_toward_remainder(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    order.refunds.create(
        provider="postfinance_prod",
        amount=Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 1, "amount": 4.65}),
    )
    remainder = order.refunds.create(
        provider="postfinance",
        amount=order.total - Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(remainder) == Decimal("7.78")


@pytest.mark.django_db
def test_externally_recorded_postfinance_refund_counts_toward_remainder(alt_event, order):
    # Refunded in the PostFinance dashboard and recorded in pretix: the
    # transaction's remaining charge is reduced just the same. No CHF amount
    # was recorded for it, so it is converted the way sending it would have.
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_EXTERNAL,
    )
    remainder = order.refunds.create(
        provider="postfinance",
        amount=order.total - Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(remainder) == Decimal("7.78")


@pytest.mark.django_db
def test_partial_refund_without_stored_rate_raises(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order, fx_rate=None)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    with pytest.raises(PaymentException):
        provider._refund_transaction_amount(refund)


@pytest.mark.django_db
def test_full_refund_without_stored_rate_uses_charged_amount(alt_event, order):
    # A full refund returns the recorded charge, so it needs no rate.
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order, fx_rate=None)
    refund = order.refunds.create(
        provider="postfinance",
        amount=order.total,
        payment=payment,
    )

    assert provider._refund_transaction_amount(refund) == Decimal("12.43")


@pytest.mark.django_db
def test_partial_refund_uses_stored_rate_not_current_setting(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order, fx_rate="0.90")
    alt_event.settings.set("payment_postfinance_alt_currency_rate", "1.10")
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(refund) == Decimal("4.50")


# Webhooks and display


@pytest.mark.django_db
def test_webhook_preserves_fx_snapshot(alt_event, order, monkeypatch, transaction_factory):
    monkeypatch.setattr(
        "pretix_postfinance.views.PostFinanceClient.get_transaction",
        lambda self, tid: transaction_factory(state=TransactionState.FULFILL),
    )

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        state=OrderPayment.PAYMENT_STATE_PENDING,
        info=json.dumps(
            {
                "transaction_id": 123456,
                "fx_rate": "0.93",
                "charged_currency": "CHF",
                "charged_amount": "12.43",
            }
        ),
    )

    status, processed = _process_transaction_webhook(123456, "12345")

    assert status == "ok"
    assert processed is True
    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert order.status == Order.STATUS_PAID
    assert payment.info_data["fx_rate"] == "0.93"


@pytest.mark.django_db
def test_payment_presale_render_shows_charged_amount(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)

    rendered = provider.payment_presale_render(payment)

    assert "12.43" in rendered
    assert "CHF" in rendered


@pytest.mark.django_db
def test_refund_control_render_shows_booked_charge(alt_event, order, rf):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    # 13.37 EUR was charged as 12.43 CHF; cancelling with a 3.00 EUR fee
    # refunds 10.37 EUR, which PostFinance booked as 9.64 CHF.
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("10.37"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 789012, "state": "SUCCESSFUL", "amount": 9.64}),
    )

    rendered = provider.refund_control_render(rf.get("/"), refund)

    assert "9.64" in rendered
    assert "CHF" in rendered
    assert "789012" in rendered
    # The transaction the refund was drawn on, so the two reconcile
    assert "123456" in rendered


@pytest.mark.django_db
def test_refund_control_render_omits_amount_for_event_currency(alt_event, order, rf):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order, fx=False)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("10.37"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 789012, "state": "SUCCESSFUL", "amount": 10.37}),
    )

    rendered = provider.refund_control_render(rf.get("/"), refund)

    # pretix already shows the only amount there is; repeating it would
    # just suggest a second, different one.
    assert "Refunded amount" not in rendered
    assert "789012" in rendered


@pytest.mark.django_db
def test_refund_control_render_shows_error(alt_event, order, rf):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("10.37"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_FAILED,
        info=json.dumps(
            {
                "error": "Refund rejected",
                "error_code": "INSUFFICIENT_FUNDS",
                "error_status_code": 422,
            }
        ),
    )

    rendered = provider.refund_control_render(rf.get("/"), refund)

    assert "Refund rejected" in rendered
    assert "INSUFFICIENT_FUNDS" in rendered
    assert str(ERROR_STATUS_MESSAGES[422]) in rendered


# Shredding


@pytest.mark.django_db
def test_shredding_keeps_the_fx_snapshot(alt_event, order):
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)

    provider.shred_payment_info(payment)

    payment.refresh_from_db()
    info = payment.info_data
    assert info["charged_currency"] == "CHF"
    assert info["charged_amount"] == "12.43"
    assert info["fx_rate"] == "0.93"
    assert info["fx_base_currency"] == "EUR"
    assert info["_shredded"] is True


@pytest.mark.django_db
def test_refund_after_shredding_stays_in_the_charged_currency(alt_event, order):
    """A shredded payment must not refund the event currency amount.

    The transaction is booked in CHF. Losing the FX snapshot used to make
    the payment look like a plain EUR one, so a 5.00 EUR refund sent 5.00
    to a CHF transaction — an over-refund of about 8%.
    """
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    provider.shred_payment_info(payment)
    payment.refresh_from_db()

    refund = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
    )

    assert provider._refund_transaction_amount(refund) == Decimal("4.65")


@pytest.mark.django_db
def test_shredded_refund_keeps_the_amount_it_booked(alt_event, order):
    # The booked CHF is what the next refund on the transaction draws
    # down, so shredding must not force it back to a re-conversion.
    provider = PostFinancePaymentProvider(alt_event)
    payment = _paid_payment(order)
    sibling = order.refunds.create(
        provider="postfinance",
        amount=Decimal("5.00"),
        payment=payment,
        state=OrderRefund.REFUND_STATE_DONE,
        info=json.dumps({"refund_id": 1, "amount": 4.65, "created_on": "2026-01-13T11:00:00Z"}),
    )

    provider.shred_payment_info(sibling)

    sibling.refresh_from_db()
    assert sibling.info_data["refund_id"] == 1
    assert sibling.info_data["amount"] == 4.65
    assert sibling.info_data.get("created_on") is None

    remainder = order.refunds.create(
        provider="postfinance",
        amount=order.total - Decimal("5.00"),
        payment=payment,
    )
    assert provider._refund_transaction_amount(remainder) == Decimal("7.78")


@pytest.mark.django_db
def test_api_error_during_execute_payment_keeps_the_fx_snapshot(
    env, rf, monkeypatch
):
    """
    An error while checking on a transaction must not cost the payment its
    FX snapshot. The customer has already been charged in the alternative
    currency by then, and a payment that loses `charged_currency` looks like
    a plain event-currency payment to `_refund_transaction_amount()` — which
    would refund the event-currency amount out of a transaction booked in
    another currency.
    """
    event, order = env
    event.settings.set("payment_postfinance_alt_currency", "CHF")
    event.settings.set("payment_postfinance_alt_currency_rate", "1.08")

    def boom(self, tid):
        raise PostFinanceError("upstream exploded", status_code=502)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction", boom
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(
        provider="postfinance",
        amount=order.total,
        info=json.dumps(
            {
                "pending_transaction_id": 515151,
                "space_id": 12345,
                "fx_rate": "1.08",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "14.44",
            }
        ),
    )

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    payment.refresh_from_db()
    assert payment.info_data["charged_currency"] == "CHF"
    assert payment.info_data["charged_amount"] == "14.44"
    assert payment.info_data["fx_rate"] == "1.08"
    assert payment.info_data["fx_base_currency"] == "EUR"
    assert payment.info_data["transaction_id"] == 515151
    assert payment.info_data["error_status_code"] == 502
    # The provisional key is redundant once transaction_id is written.
    assert "pending_transaction_id" not in payment.info_data


@pytest.mark.django_db
def test_refund_after_a_failed_status_check_still_converts(env, rf, monkeypatch):
    """
    The point of keeping the snapshot: the refund that follows such an error
    must still be sent in the currency the transaction was booked in.
    """
    event, order = env
    event.settings.set("payment_postfinance_alt_currency", "CHF")
    event.settings.set("payment_postfinance_alt_currency_rate", "1.08")

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_transaction",
        lambda self, tid: (_ for _ in ()).throw(
            PostFinanceError("upstream exploded", status_code=502)
        ),
    )

    prov = PostFinancePaymentProvider(event)
    req = rf.get("/")
    req.session = {}

    payment = order.payments.create(
        provider="postfinance",
        amount=Decimal("13.37"),
        info=json.dumps(
            {
                "pending_transaction_id": 515151,
                "space_id": 12345,
                "fx_rate": "1.08",
                "fx_base_currency": "EUR",
                "charged_currency": "CHF",
                "charged_amount": "14.44",
            }
        ),
    )

    with pytest.raises(PaymentException):
        prov.execute_payment(req, payment)

    payment.refresh_from_db()
    refund = order.refunds.create(
        provider="postfinance",
        payment=payment,
        amount=Decimal("5.00"),
        state=OrderRefund.REFUND_STATE_CREATED,
    )

    # 5.00 EUR at the snapshotted rate, not 5.00 sent to a CHF transaction.
    assert prov._refund_transaction_amount(refund) == Decimal("5.40")
