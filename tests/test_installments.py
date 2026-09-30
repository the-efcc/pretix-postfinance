"""
Tests for PostFinance installment payments.

These exercise the provider against stand-in plan and installment objects
rather than pretix's models, so they also run on an installation without
installment support. ``test_installments_integration.py`` covers the same
code against the real models.
"""

from __future__ import annotations

import pathlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from postfinancecheckout.models import ChargeState, TransactionState

from pretix_postfinance.api import PostFinanceError
from pretix_postfinance.payment import PostFinancePaymentProvider


@pytest.fixture
def chf_event(event):
    event.currency = "CHF"
    event.save()
    return event


@pytest.fixture
def alt_currency_event(chf_event):
    chf_event.settings.set("payment_postfinance_alt_currency", "EUR")
    chf_event.settings.set("payment_postfinance_alt_currency_rate", "1.07")
    return chf_event


def make_plan(event, order, *, token=None, total_installments=3):
    """A stand-in for ``InstallmentPlan`` with the fields the provider reads."""
    plan = SimpleNamespace(
        pk=42,
        order=order,
        total_installments=total_installments,
        payment_token=token if token is not None else {},
        payment_provider="postfinance",
    )
    plan.store_payment_token = lambda data: setattr(plan, "payment_token", data)
    return plan


def make_payment(order, *, amount="100.00"):
    """A stand-in for the `created` OrderPayment pretix hands the provider."""
    return SimpleNamespace(pk=7, order=order, amount=Decimal(amount), info_data={})


def make_installment(plan, *, number=2, amount="100.00"):
    """A stand-in for ``ScheduledInstallment``."""
    installment = SimpleNamespace(
        pk=number,
        plan=plan,
        installment_number=number,
        amount=Decimal(amount),
        failure_reason=None,
        saved_fields=[],
    )
    installment.save = lambda update_fields=None: installment.saved_fields.append(
        tuple(update_fields or ())
    )
    return installment


@pytest.fixture
def charge_calls(monkeypatch):
    """Capture the transaction the provider creates for a token charge."""
    calls: dict[str, object] = {}

    def create_transaction(self, **kwargs):
        calls["create"] = kwargs
        calls["space_id"] = self.space_id
        return SimpleNamespace(id=234567, state=TransactionState.PENDING)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        create_transaction,
    )
    return calls


def charged_transaction(state=TransactionState.FULFILL):
    return SimpleNamespace(
        id=234567,
        state=state,
        payment_connector_configuration=SimpleNamespace(name="TWINT"),
        created_on="2026-01-13T10:00:00Z",
    )


def successful_charge(monkeypatch, transaction=None):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.SUCCESSFUL,
            failure_reason=None,
            transaction=transaction if transaction is not None else charged_transaction(),
        ),
    )


STORED_TOKEN = {
    "token_id": 999888,
    "customer_id": "cus_test123",
    "customer_email": "customer@example.org",
    "space_id": "12345",
}


@pytest.mark.django_db
def test_installments_supported_when_pretix_provides_them(event):
    prov = PostFinancePaymentProvider(event)
    pytest.importorskip("pretix.efcc.models")
    assert prov.installments_supported is True


@pytest.mark.django_db
def test_execute_installment_charges_event_currency(
    chf_event, order, monkeypatch, charge_calls
):
    successful_charge(monkeypatch)
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan, number=2, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is True

    create = charge_calls["create"]
    assert create["currency"] == "CHF"
    assert create["token"] == 999888
    assert create["line_items"][0].amount_including_tax == 100.00
    assert create["merchant_reference"] == f"{chf_event.slug}-{order.code}-inst-2"
    assert installment.failure_reason is None


@pytest.mark.django_db
def test_execute_installment_charges_alternative_currency(
    alt_currency_event, order, monkeypatch, charge_calls
):
    """An installment of a plan sold in EUR is charged in EUR."""
    successful_charge(monkeypatch)
    plan = make_plan(
        alt_currency_event,
        order,
        token={**STORED_TOKEN, "charged_currency": "EUR", "fx_rate": "1.07"},
    )
    installment = make_installment(plan, number=2, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(alt_currency_event)
    assert prov.execute_installment(plan, installment, payment) is True

    create = charge_calls["create"]
    assert create["currency"] == "EUR"
    assert create["line_items"][0].amount_including_tax == 107.00


@pytest.mark.django_db
def test_installment_uses_snapshotted_rate_not_current_setting(
    alt_currency_event, order, monkeypatch, charge_calls
):
    """
    A rate change after the order must not reprice open installments.
    """
    successful_charge(monkeypatch)
    alt_currency_event.settings.set("payment_postfinance_alt_currency_rate", "2.00")
    plan = make_plan(
        alt_currency_event,
        order,
        token={**STORED_TOKEN, "charged_currency": "EUR", "fx_rate": "1.07"},
    )
    installment = make_installment(plan, number=3, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(alt_currency_event)
    assert prov.execute_installment(plan, installment, payment) is True

    assert charge_calls["create"]["line_items"][0].amount_including_tax == 107.00


@pytest.mark.django_db
def test_installment_falls_back_to_configured_rate(
    alt_currency_event, order, monkeypatch, charge_calls
):
    """
    A plan predating the snapshot converts at the currently configured rate,
    which is the only rate on record for it.
    """
    successful_charge(monkeypatch)
    plan = make_plan(
        alt_currency_event, order, token={**STORED_TOKEN, "charged_currency": "EUR"}
    )
    installment = make_installment(plan, number=2, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(alt_currency_event)
    assert prov.execute_installment(plan, installment, payment) is True

    assert charge_calls["create"]["currency"] == "EUR"
    assert charge_calls["create"]["line_items"][0].amount_including_tax == 107.00


@pytest.mark.django_db
def test_installment_fails_when_rate_is_unknown(chf_event, order, monkeypatch):
    """
    Without a rate, the amount to charge cannot be derived — the installment
    fails with a reason instead of being charged something made up.
    """
    created = MagicMock()
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: created,
    )
    plan = make_plan(chf_event, order, token={**STORED_TOKEN, "charged_currency": "EUR"})
    installment = make_installment(plan, number=2, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert "exchange rate" in installment.failure_reason
    created.assert_not_called()


@pytest.mark.django_db
def test_installment_rounds_each_share_independently(
    alt_currency_event, order, monkeypatch, charge_calls
):
    """Each installment converts its own amount, half-up."""
    successful_charge(monkeypatch)
    plan = make_plan(
        alt_currency_event,
        order,
        token={**STORED_TOKEN, "charged_currency": "EUR", "fx_rate": "1.07"},
    )
    installment = make_installment(plan, number=3, amount="33.34")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(alt_currency_event)
    assert prov.execute_installment(plan, installment, payment) is True

    # 33.34 * 1.07 = 35.6738
    assert charge_calls["create"]["line_items"][0].amount_including_tax == 35.67


@pytest.mark.django_db
def test_charge_is_recorded_on_the_payment(chf_event, order, monkeypatch, charge_calls):
    """
    pretix persists what we leave on the payment. Without the transaction
    reference and a refundable state the charge could not be refunded
    through pretix at all.
    """
    successful_charge(monkeypatch)
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is True

    info = payment.info_data
    assert info["transaction_id"] == 234567
    assert info["space_id"] == 12345
    assert info["token_id"] == 999888
    assert info["state"] == TransactionState.FULFILL.value
    assert info["payment_method"] == "TWINT"
    assert info["installment_charge"] is True

    # ... which is what makes the payment refundable.
    order.payments.create(provider="postfinance", amount=installment.amount)
    recorded = order.payments.last()
    recorded.info_data = info
    assert prov.payment_refund_supported(recorded) is True


@pytest.mark.django_db
def test_alternative_currency_charge_is_recorded_on_the_payment(
    alt_currency_event, order, monkeypatch, charge_calls
):
    """
    A refund of this payment has to convert back at the rate it was charged
    at, so the snapshot goes on the payment exactly as an interactive
    payment records it.
    """
    successful_charge(monkeypatch)
    plan = make_plan(
        alt_currency_event,
        order,
        token={**STORED_TOKEN, "charged_currency": "EUR", "fx_rate": "1.07"},
    )
    installment = make_installment(plan, number=2, amount="100.00")
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(alt_currency_event)
    assert prov.execute_installment(plan, installment, payment) is True

    info = payment.info_data
    assert info["charged_currency"] == "EUR"
    assert info["charged_amount"] == "107.00"
    assert info["fx_rate"] == "1.07"
    assert info["fx_base_currency"] == "CHF"


@pytest.mark.django_db
def test_event_currency_charge_records_no_exchange_rate(
    chf_event, order, monkeypatch, charge_calls
):
    successful_charge(monkeypatch)
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    PostFinancePaymentProvider(chf_event).execute_installment(plan, installment, payment)

    assert "charged_currency" not in payment.info_data
    assert "fx_rate" not in payment.info_data


@pytest.mark.django_db
def test_transaction_is_recorded_before_the_charge_is_made(
    chf_event, order, monkeypatch, charge_calls
):
    """
    A charge that dies after the money moved must still leave the payment
    naming the transaction, or it can neither be reconciled nor refunded.
    """
    seen = {}

    def die_after_charging(self, tid):
        seen["info"] = dict(payment.info_data)
        raise PostFinanceError("gateway timeout", status_code=504)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        die_after_charging,
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert seen["info"]["transaction_id"] == 234567
    assert payment.info_data["transaction_id"] == 234567
    assert payment.info_data["error"] == "gateway timeout"


@pytest.mark.django_db
def test_failed_charge_is_recorded_on_the_payment(
    chf_event, order, monkeypatch, charge_calls
):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.FAILED,
            failure_reason=SimpleNamespace(description="Insufficient funds"),
            transaction=charged_transaction(state=TransactionState.FAILED),
        ),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert payment.info_data["error"] == "Insufficient funds"
    assert payment.info_data["state"] == TransactionState.FAILED.value
    assert payment.info_data["transaction_id"] == 234567


@pytest.mark.django_db
def test_execute_installment_without_token(chf_event, order):
    plan = make_plan(chf_event, order, token={})
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False
    assert installment.failure_reason == (
        "No stored payment method is available for this plan."
    )


@pytest.mark.django_db
def test_execute_installment_records_charge_failure(
    chf_event, order, monkeypatch, charge_calls
):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.FAILED,
            failure_reason=SimpleNamespace(description="Insufficient funds"),
            transaction=charged_transaction(state=TransactionState.FAILED),
        ),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False
    assert installment.failure_reason == "Insufficient funds"


@pytest.mark.django_db
def test_execute_installment_records_pending_charge_as_failure(
    chf_event, order, monkeypatch, charge_calls
):
    """
    A charge that is neither successful nor failed has not moved money, so
    pretix must not be told the installment is paid.
    """
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.PENDING,
            failure_reason=None,
            transaction=charged_transaction(state=TransactionState.PENDING),
        ),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False
    assert installment.failure_reason == (
        "The payment did not complete (PostFinance state: PENDING)."
    )


@pytest.mark.django_db
def test_execute_installment_survives_api_error(chf_event, order, monkeypatch):
    def boom(self, **kwargs):
        raise PostFinanceError("PostFinance is down", status_code=503)

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction", boom
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False
    assert installment.failure_reason == "PostFinance is down"


@pytest.mark.django_db
def test_execute_installment_without_transaction_id(chf_event, order, monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.create_transaction",
        lambda self, **kwargs: SimpleNamespace(id=None),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False
    assert installment.failure_reason == (
        "PostFinance did not return a transaction reference."
    )


@pytest.mark.django_db
def test_installment_charged_in_the_space_that_issued_the_token(
    chf_event, order, monkeypatch, charge_calls
):
    """
    A token only works in the space it was created in, so the recorded space
    decides which credentials are used — not the event's current test mode.
    """
    successful_charge(monkeypatch)
    chf_event.settings.set("payment_postfinance_test_space_id", "54321")
    chf_event.settings.set("payment_postfinance_test_user_id", "67890")
    chf_event.settings.set("payment_postfinance_test_auth_key", "test-secret")
    plan = make_plan(chf_event, order, token={**STORED_TOKEN, "space_id": "54321"})
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is True
    assert charge_calls["space_id"] == 54321


@pytest.mark.django_db
def test_revoke_payment_token(chf_event, order, monkeypatch):
    deleted = []
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.delete_token",
        lambda self, tid: deleted.append(tid),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))

    PostFinancePaymentProvider(chf_event).revoke_payment_token(plan)
    assert deleted == [999888]


@pytest.mark.django_db
def test_revoke_payment_token_without_token(chf_event, order, monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.delete_token",
        lambda self, tid: pytest.fail("nothing to revoke"),
    )
    plan = make_plan(chf_event, order, token={})

    PostFinancePaymentProvider(chf_event).revoke_payment_token(plan)


@pytest.mark.django_db
def test_revoke_payment_token_survives_api_error(chf_event, order, monkeypatch):
    """
    pretix clears the plan either way, so a failed revocation must not stop
    the plan from being closed.
    """

    def boom(self, tid):
        raise PostFinanceError("token already gone", status_code=404)

    monkeypatch.setattr("pretix_postfinance.payment.PostFinanceClient.delete_token", boom)
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))

    PostFinancePaymentProvider(chf_event).revoke_payment_token(plan)



# The reasons `_fail_installment()` is given for a failure that is ours to
# describe. PostFinance's own wording, where it supplies any, is passed
# through untouched and so is deliberately absent from this list.
TRANSLATABLE_FAILURE_REASONS = (
    "An earlier payment attempt could not be checked.",
    "An earlier payment attempt is still being processed.",
    "No stored payment method is available for this plan.",
    "PostFinance did not return a transaction reference.",
    "The payment was declined by PostFinance.",
    "The payment did not complete (PostFinance state: {state}).",
    "The payment did not complete.",
)


def _msgstr(entry: str) -> str:
    """
    The translation an entry carries, joined from however many lines it spans.

    gettext wraps a long translation onto continuation lines after an empty
    `msgstr ""`, so reading only the first line reports a perfectly good
    translation as missing.
    """
    lines = entry.split("\n")
    assert lines[0].startswith("msgstr ")
    parts = [lines[0][len("msgstr ") :]]
    for line in lines[1:]:
        line = line.strip()
        if not line.startswith('"'):
            break
        parts.append(line)
    return "".join(part.strip().strip('"') for part in parts)


@pytest.mark.parametrize("language", ["de", "fr", "it", "es"])
@pytest.mark.parametrize("reason", TRANSLATABLE_FAILURE_REASONS)
def test_failure_reasons_are_in_every_catalog(language, reason):
    """
    A failure reason is what pretix shows the customer and the organizer, so
    it has to be translated like every other user-facing string here. The
    `.mo` files are built by pretix-plugin-build and are not in the source
    tree, so asserting the translated output at runtime is not possible —
    what is checkable, and what actually goes wrong, is a string reaching the
    code without reaching the catalogs.
    """
    catalog = pathlib.Path(
        f"pretix_postfinance/locale/{language}/LC_MESSAGES/django.po"
    ).read_text(encoding="utf-8")

    assert f'msgid "{reason}"' in catalog, f"{reason!r} missing from the {language} catalog"

    entry = catalog.split(f'msgid "{reason}"\n', 1)[1]
    assert _msgstr(entry), f"{reason!r} is untranslated in {language}"


@pytest.mark.django_db
def test_missing_token_reason_uses_the_translatable_wording(chf_event, order):
    plan = make_plan(chf_event, order, token={})
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert installment.failure_reason == "No stored payment method is available for this plan."
    # The same reason reaches the payment, which is what pretix fails it with.
    assert payment.info_data["error"] == installment.failure_reason


@pytest.mark.django_db
def test_postfinance_wording_is_passed_through(chf_event, order, monkeypatch, charge_calls):
    """
    Where PostFinance supplies its own reason it is used as it comes: it
    describes the actual decline, which no wording of ours would improve on.
    """
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.FAILED,
            failure_reason=SimpleNamespace(description="Insufficient funds"),
            transaction=charged_transaction(state=TransactionState.FAILED),
        ),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert installment.failure_reason == "Insufficient funds"


@pytest.mark.django_db
def test_decline_without_a_reason_falls_back_to_our_wording(
    chf_event, order, monkeypatch, charge_calls
):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.process_with_token",
        lambda self, tid: SimpleNamespace(
            state=ChargeState.FAILED,
            failure_reason=None,
            transaction=charged_transaction(state=TransactionState.FAILED),
        ),
    )
    plan = make_plan(chf_event, order, token=dict(STORED_TOKEN))
    installment = make_installment(plan)
    payment = make_payment(order)

    prov = PostFinancePaymentProvider(chf_event)
    assert prov.execute_installment(plan, installment, payment) is False

    assert installment.failure_reason == "The payment was declined by PostFinance."
