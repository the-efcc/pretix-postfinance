import inspect
import os

os.environ["PRETIX_POSTFINANCE_TESTING"] = "1"

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from django.test import RequestFactory
from django.utils import translation
from django.utils.timezone import now
from django_scopes import scopes_disabled
from postfinancecheckout.models import TransactionState
from pretix.base.models import Event, Order, Organizer


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if inspect.isgeneratorfunction(fixturedef.func):
        yield
    else:
        with scopes_disabled():
            yield


@pytest.fixture(autouse=True)
def reset_locale():
    translation.activate("en")


@pytest.fixture(autouse=True)
def no_messages(monkeypatch):
    monkeypatch.setattr("django.contrib.messages.api.add_message", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def disable_scopes():
    with scopes_disabled():
        yield


# Request factory


@pytest.fixture
def rf():
    return RequestFactory()


# Database fixtures


@pytest.fixture
def organizer():
    return Organizer.objects.create(name="Dummy", slug="dummy")


@pytest.fixture
def event(organizer):
    event = Event.objects.create(
        organizer=organizer,
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
    return event


@pytest.fixture
def order(event, organizer):
    return Order.objects.create(
        code="FOOBAR",
        event=event,
        email="dummy@dummy.test",
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("13.37"),
        sales_channel=organizer.sales_channels.get(identifier="web"),
    )


@pytest.fixture
def env(event, order):
    return event, order


# Test mode fixtures


@pytest.fixture
def testmode_organizer():
    return Organizer.objects.create(name="TestOrg", slug="testorg")


@pytest.fixture
def testmode_event(testmode_organizer):
    event = Event.objects.create(
        organizer=testmode_organizer,
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
    return event


@pytest.fixture
def testmode_order(testmode_event, testmode_organizer):
    return Order.objects.create(
        code="TESTORDER",
        event=testmode_event,
        email="test@test.test",
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("10.00"),
        sales_channel=testmode_organizer.sales_channels.get(identifier="web"),
    )


@pytest.fixture
def testmode_env(testmode_event, testmode_order):
    return testmode_event, testmode_order


# Factory fixtures


@pytest.fixture
def transaction_factory():
    def _create(
        id: int = 123456,
        state: TransactionState = TransactionState.COMPLETED,
        payment_method: str = "TWINT",
        created_on: str = "2026-01-13T10:00:00Z",
        amount: float = 100.00,
    ):
        transaction = MagicMock()
        transaction.id = id
        transaction.state = state
        transaction.created_on = created_on
        transaction.amount = amount
        transaction.payment_connector_configuration = MagicMock()
        transaction.payment_connector_configuration.name = payment_method
        return transaction

    return _create


@pytest.fixture
def refund_factory():
    def _create(
        id: int = 789012,
        state: str = "SUCCESSFUL",
        amount: float = 50.00,
        created_on: str = "2026-01-13T11:00:00Z",
    ):
        refund = MagicMock()
        refund.id = id
        refund.state = MagicMock()
        refund.state.value = state
        refund.amount = amount
        refund.created_on = created_on
        return refund

    return _create


@pytest.fixture
def space_factory():
    def _create(id: int = 12345, name: str = "Test Space"):
        space = MagicMock()
        space.id = id
        space.name = name
        return space

    return _create


# Convenience fixtures using factories


@pytest.fixture
def mock_postfinance_config():
    return {
        "space_id": "12345",
        "user_id": "67890",
        "api_secret": "test-secret-key",
    }


@pytest.fixture
def mock_transaction(transaction_factory):
    return transaction_factory()


@pytest.fixture
def mock_refund(refund_factory):
    return refund_factory()


@pytest.fixture
def mock_space(space_factory):
    return space_factory()


@pytest.fixture
def mock_order_payment():
    payment = MagicMock()
    payment.pk = 1
    payment.amount = Decimal("100.00")
    payment.state = "created"
    payment.info_data = {}
    payment.order = MagicMock()
    payment.order.code = "ABC12"
    payment.order.event = MagicMock()
    payment.order.event.currency = "CHF"
    payment.order.event.slug = "test-event"
    payment.payment_provider = MagicMock()
    return payment


@pytest.fixture
def mock_request():
    request = MagicMock()
    request.session = {}
    request.META = {"CSRF_COOKIE": "test-csrf-token"}
    request.POST = {}
    request.headers = {}
    request.body = b"{}"
    request.content_type = "application/json"
    return request


@pytest.fixture
def mock_event():
    event = MagicMock()
    event.slug = "test-event"
    event.currency = "CHF"
    event.organizer = MagicMock()
    event.organizer.slug = "test-org"
    event.settings = MagicMock()
    return event
