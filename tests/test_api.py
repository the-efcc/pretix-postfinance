import json
import os
import socket
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

import pretix_postfinance.api as api_module
from pretix_postfinance.api import (
    PostFinanceClient,
    PostFinanceError,
    _get_timeout,
)


class TestPostFinanceError:
    @pytest.mark.parametrize(
        ("message", "status_code", "error_code"),
        [
            ("Test error", None, None),
            ("Auth failed", 401, None),
            ("Not found", 404, "RESOURCE_NOT_FOUND"),
        ],
    )
    def test_error_attributes(self, message, status_code, error_code):
        error = PostFinanceError(message, status_code=status_code, error_code=error_code)
        assert str(error) == message
        assert error.message == message
        assert error.status_code == status_code
        assert error.error_code == error_code


@pytest.fixture
def mock_services():
    """Mock all PostFinance SDK services to allow client instantiation."""
    mocks = {
        "Configuration": MagicMock(),
        "SpacesService": MagicMock(),
        "TransactionsService": MagicMock(),
        "RefundsService": MagicMock(),
        "TokensService": MagicMock(),
        "WebhookEncryptionKeysService": MagicMock(),
        "PaymentMethodConfigurationsService": MagicMock(),
        "WebhookURLsService": MagicMock(),
        "WebhookListenersService": MagicMock(),
    }
    with (
        patch.object(api_module, "Configuration", mocks["Configuration"]),
        patch.object(api_module, "SpacesService", mocks["SpacesService"]),
        patch.object(api_module, "TransactionsService", mocks["TransactionsService"]),
        patch.object(api_module, "RefundsService", mocks["RefundsService"]),
        patch.object(api_module, "TokensService", mocks["TokensService"]),
        patch.object(
            api_module, "WebhookEncryptionKeysService", mocks["WebhookEncryptionKeysService"]
        ),
        patch.object(
            api_module,
            "PaymentMethodConfigurationsService",
            mocks["PaymentMethodConfigurationsService"],
        ),
        patch.object(api_module, "WebhookURLsService", mocks["WebhookURLsService"]),
        patch.object(api_module, "WebhookListenersService", mocks["WebhookListenersService"]),
    ):
        yield mocks


class TestPostFinanceClient:
    def test_client_initialization(self, mock_services):  # noqa: ARG002
        client = PostFinanceClient(
            space_id=12345,
            user_id=67890,
            api_secret="test-secret",
        )
        assert client.space_id == 12345
        assert client.user_id == 67890
        assert client.api_secret == "test-secret"

    def test_get_space_success(self, mock_services, mock_space):
        mock_spaces_instance = MagicMock()
        mock_spaces_instance.get_spaces_id.return_value = mock_space
        mock_services["SpacesService"].return_value = mock_spaces_instance

        client = PostFinanceClient(
            space_id=12345,
            user_id=67890,
            api_secret="test-secret",
        )

        result = client.get_space()

        assert result == mock_space
        mock_spaces_instance.get_spaces_id.assert_called_once_with(id=12345)

    def test_get_space_api_exception(self, mock_services):
        from postfinancecheckout.exceptions import ApiException

        mock_spaces_instance = MagicMock()
        mock_api_error = ApiException(status=401, reason="Unauthorized")
        mock_spaces_instance.get_spaces_id.side_effect = mock_api_error
        mock_services["SpacesService"].return_value = mock_spaces_instance

        client = PostFinanceClient(
            space_id=12345,
            user_id=67890,
            api_secret="test-secret",
        )

        with pytest.raises(PostFinanceError) as exc_info:
            client.get_space()

        assert exc_info.value.status_code == 401

    def test_get_transaction_success(self, mock_services, mock_transaction):
        mock_transactions_instance = MagicMock()
        mock_transactions_instance.get_payment_transactions_id.return_value = mock_transaction
        mock_services["TransactionsService"].return_value = mock_transactions_instance

        client = PostFinanceClient(
            space_id=12345,
            user_id=67890,
            api_secret="test-secret",
        )

        result = client.get_transaction(123456)

        assert result == mock_transaction
        mock_transactions_instance.get_payment_transactions_id.assert_called_once_with(
            id=123456, space=12345
        )

    def test_get_refund_success(self, mock_services, mock_refund):
        mock_refunds_instance = MagicMock()
        mock_refunds_instance.get_payment_refunds_id.return_value = mock_refund
        mock_services["RefundsService"].return_value = mock_refunds_instance

        client = PostFinanceClient(
            space_id=12345,
            user_id=67890,
            api_secret="test-secret",
        )

        result = client.get_refund(789012)

        assert result == mock_refund
        mock_refunds_instance.get_payment_refunds_id.assert_called_once_with(id=789012, space=12345)


class TestGetTimeout:
    @pytest.mark.parametrize(
        ("env_vars", "expected"),
        [
            ({}, 15),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "20"}, 20),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "abc"}, 15),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "0"}, 15),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "-5"}, 15),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "500"}, 300),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "300"}, 300),
            ({"PRETIX_POSTFINANCE_API_TIMEOUT": "1"}, 1),
        ],
    )
    def test_timeout_values(self, env_vars, expected):
        with patch.dict(os.environ, env_vars, clear=True):
            assert _get_timeout() == expected


class _FakePostFinance(BaseHTTPRequestHandler):
    """
    Stands in for PostFinance, so that the real SDK and urllib3 run.

    Each POST is answered with the next entry of `answers`. Like PostFinance,
    it creates the refund before answering, whatever the answer, and answers
    a repeated external ID with the refund it already created.
    """

    answers: ClassVar[list[str]] = []
    refunds: ClassVar[dict[str, dict]] = {}
    requests: ClassVar[list[dict]] = []

    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The client stopped waiting, as a timeout test wants it to.

    def do_POST(self):
        refund_create = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(refund_create)
        answer = self.answers.pop(0)
        if answer == "400":
            self._send(400, {"message": "Amount exceeds the refundable amount"})
            return
        external_id = refund_create.get("externalId")
        refund = self.refunds.setdefault(
            external_id,
            {"id": 555000 + len(self.refunds), "state": "PENDING", "externalId": external_id},
        )
        if answer == "slow":
            # Longer than the client waits; the refund is created regardless.
            time.sleep(2)
        if answer == "502":
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b"Bad Gateway")
            return
        self._send(201, refund)


@pytest.fixture
def fake_postfinance(monkeypatch):
    _FakePostFinance.answers = []
    _FakePostFinance.refunds = {}
    _FakePostFinance.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakePostFinance)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(PostFinanceClient, "DEFAULT_TIMEOUT", 1)
    client = PostFinanceClient(
        space_id=12345, user_id=67890, api_secret="dGVzdC1zZWNyZXQtdGVzdC1zZWNyZXQ="
    )
    client._configuration.host = f"http://127.0.0.1:{server.server_address[1]}/api/v2.0"
    yield client, _FakePostFinance
    server.shutdown()
    server.server_close()


def _refund(client):
    return client.refund_transaction(
        transaction_id=123456, external_id="pretix-org-ev-ABC12-R-1", amount=Decimal("5.00")
    )


class TestRefundTransactionOutcome:
    @pytest.mark.parametrize("first_answer", ["502", "slow"], ids=["gateway-error", "timeout"])
    def test_a_refund_that_may_have_been_created_is_asked_for_again(
        self, fake_postfinance, first_answer
    ):
        client, fake = fake_postfinance
        fake.answers = [first_answer, "201"]

        refund = _refund(client)

        assert refund.id == 555000
        assert len(fake.requests) == 2
        assert fake.requests[0] == fake.requests[1]
        assert len(fake.refunds) == 1

    def test_a_rejected_refund_is_not_sent_again(self, fake_postfinance):
        client, fake = fake_postfinance
        fake.answers = ["400"]

        with pytest.raises(PostFinanceError) as exc_info:
            _refund(client)

        assert exc_info.value.status_code == 400
        assert not exc_info.value.outcome_unknown
        assert len(fake.requests) == 1

    @pytest.mark.parametrize("second_answer", ["502", "slow", "400"])
    def test_a_refund_still_unconfirmed_is_reported_unknown(
        self, fake_postfinance, second_answer
    ):
        client, fake = fake_postfinance
        fake.answers = ["502", second_answer]

        with pytest.raises(PostFinanceError) as exc_info:
            _refund(client)

        assert exc_info.value.outcome_unknown
        assert len(fake.requests) == 2

    def test_a_refund_that_never_left_is_not_unknown(self, fake_postfinance):
        client, _ = fake_postfinance
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            port = unused.getsockname()[1]
        client._configuration.host = f"http://127.0.0.1:{port}/api/v2.0"

        with pytest.raises(PostFinanceError) as exc_info:
            _refund(client)

        assert exc_info.value.connection_error
        assert not exc_info.value.outcome_unknown


def test_a_timeout_on_any_call_is_a_postfinance_error(fake_postfinance):
    client, fake = fake_postfinance
    fake.answers = ["slow"]

    with pytest.raises(PostFinanceError) as exc_info:
        client.create_transaction(currency="CHF", line_items=[])

    assert exc_info.value.connection_error
    assert exc_info.value.status_code is None
