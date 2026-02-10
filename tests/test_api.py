import os
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
