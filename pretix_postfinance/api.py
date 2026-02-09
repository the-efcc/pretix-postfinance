"""
PostFinance Checkout API client.

Provides a wrapper around the official PostFinance Checkout Python SDK
for use with the pretix payment plugin.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from postfinancecheckout import Configuration
from postfinancecheckout.exceptions import ApiException
from postfinancecheckout.models import (
    CreationEntityState,
    LineItemCreate,
    PaymentMethodConfiguration,
    Refund,
    RefundCreate,
    RefundState,
    RefundType,
    Space,
    Transaction,
    TransactionCreate,
    TransactionState,
    WebhookListener,
    WebhookListenerCreate,
    WebhookUrl,
    WebhookUrlCreate,
)
from postfinancecheckout.postfinancecheckout_sdk_exception import (
    PostFinanceCheckoutSdkException,
)
from postfinancecheckout.service import (
    PaymentMethodConfigurationsService,
    RefundsService,
    SpacesService,
    TransactionsService,
    WebhookEncryptionKeysService,
    WebhookListenersService,
    WebhookURLsService,
)

logger = logging.getLogger(__name__)


def _get_timeout() -> int:
    """
    Get API timeout from environment variable.

    Reads PRETIX_POSTFINANCE_API_TIMEOUT and validates it.
    Returns default of 15 seconds if not set or invalid.
    Caps at 300 seconds maximum.
    """
    default = 15
    env_value = os.environ.get("PRETIX_POSTFINANCE_API_TIMEOUT")

    if env_value is None:
        return default

    try:
        timeout = int(env_value)
    except ValueError:
        logger.warning(
            "Invalid PRETIX_POSTFINANCE_API_TIMEOUT value '%s', using default %d",
            env_value,
            default,
        )
        return default

    if timeout <= 0:
        logger.warning(
            "PRETIX_POSTFINANCE_API_TIMEOUT must be positive, using default %d",
            default,
        )
        return default

    if timeout > 300:
        logger.warning(
            "PRETIX_POSTFINANCE_API_TIMEOUT capped at 300 seconds (was %d)",
            timeout,
        )
        return 300

    return timeout


class PostFinanceError(Exception):
    """Base exception for PostFinance API errors."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class PostFinanceClient:
    """
    Client for PostFinance Checkout API using the official SDK.

    Provides a simplified interface to the PostFinance Checkout SDK
    for common payment operations.

    Attributes:
        space_id: The PostFinance space ID.
        user_id: The PostFinance user ID for authentication.
        api_secret: The API secret (authentication key).
    """

    DEFAULT_TIMEOUT = _get_timeout()

    def __init__(
        self,
        space_id: int,
        user_id: int,
        api_secret: str,
    ) -> None:
        """
        Initialize the PostFinance API client.

        Args:
            space_id: The PostFinance space ID.
            user_id: The PostFinance user ID for authentication.
            api_secret: The API secret (authentication key).
        """
        self.space_id = space_id
        self.user_id = user_id
        self.api_secret = api_secret

        self._configuration = Configuration(
            user_id=user_id,
            authentication_key=api_secret,
            request_timeout=self.DEFAULT_TIMEOUT,
        )
        self._spaces_service = SpacesService(self._configuration)
        self._transactions_service = TransactionsService(self._configuration)
        self._refunds_service = RefundsService(self._configuration)
        self._webhook_encryption_service = WebhookEncryptionKeysService(self._configuration)
        self._payment_method_configs_service = PaymentMethodConfigurationsService(
            self._configuration
        )
        self._webhook_url_service = WebhookURLsService(self._configuration)
        self._webhook_listener_service = WebhookListenersService(self._configuration)

    def get_space(self) -> Space:
        """
        Get details about the configured space.

        This is useful for testing the connection and verifying credentials.

        Returns:
            The Space object with id, name, and other details.

        Raises:
            PostFinanceError: If the request fails or credentials are invalid.
        """
        try:
            return self._spaces_service.get_spaces_id(id=self.space_id)
        except ApiException as e:
            logger.error("PostFinance API error getting space: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting space: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_payment_method_configurations(self) -> list[PaymentMethodConfiguration]:
        """
        Get all active payment method configurations for the space.

        Returns:
            List of PaymentMethodConfiguration objects that are active.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            response = self._payment_method_configs_service.get_payment_method_configurations(
                space=self.space_id,
                limit=100,
            )
            # Filter to only return active configurations
            return [
                config
                for config in (response.data or [])
                if config.state == CreationEntityState.ACTIVE
            ]
        except ApiException as e:
            logger.error("PostFinance API error getting payment method configurations: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting payment method configurations: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def create_transaction(
        self,
        currency: str,
        line_items: list[LineItemCreate],
        success_url: str,
        failed_url: str,
        merchant_reference: str | None = None,
        language: str | None = None,
        allowed_payment_method_configurations: list[int] | None = None,
    ) -> Transaction:
        """
        Create a new payment transaction.

        Args:
            currency: The three-letter currency code (e.g., 'CHF', 'EUR').
            line_items: List of LineItemCreate objects for the transaction.
            success_url: URL to redirect to on successful payment.
            failed_url: URL to redirect to on failed/cancelled payment.
            merchant_reference: Optional merchant reference for this transaction.
            language: Optional language code for the payment page (e.g., 'en-US').
            allowed_payment_method_configurations: Optional list of payment method
                configuration IDs to restrict which payment methods are available.
                If not provided, all configured payment methods are available.

        Returns:
            The created Transaction object.

        Raises:
            PostFinanceError: If the request fails.
        """
        transaction_create = TransactionCreate(
            currency=currency,
            lineItems=line_items,
            successUrl=success_url,
            failedUrl=failed_url,
            merchantReference=merchant_reference,
            language=language,
            allowedPaymentMethodConfigurations=allowed_payment_method_configurations,
        )

        try:
            return self._transactions_service.post_payment_transactions(
                space=self.space_id,
                transaction_create=transaction_create,
            )
        except ApiException as e:
            logger.error("PostFinance API error creating transaction: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error creating transaction: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_payment_page_url(self, transaction_id: int) -> str:
        """
        Get the URL for the payment page for a transaction.

        Args:
            transaction_id: The ID of the transaction.

        Returns:
            The URL to redirect the customer to for payment.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            return self._transactions_service.get_payment_transactions_id_payment_page_url(
                id=transaction_id,
                space=self.space_id,
            )
        except ApiException as e:
            logger.error("PostFinance API error getting payment page URL: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting payment page URL: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_transaction(self, transaction_id: int) -> Transaction:
        """
        Retrieve a transaction by its ID.

        Args:
            transaction_id: The ID of the transaction.

        Returns:
            The Transaction object.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            return self._transactions_service.get_payment_transactions_id(
                id=transaction_id,
                space=self.space_id,
            )
        except ApiException as e:
            logger.error("PostFinance API error getting transaction: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting transaction: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def refund_transaction(
        self,
        transaction_id: int,
        external_id: str,
        merchant_reference: str | None = None,
        amount: Decimal | float | None = None,
    ) -> Refund:
        """
        Create a refund for a completed transaction.

        This creates a refund for a transaction that is in the COMPLETED or
        FULFILL state. If no amount is specified, a full refund is created.

        Args:
            transaction_id: The ID of the transaction to refund.
            external_id: A unique client-generated ID for this refund request.
                Subsequent requests with the same ID will not execute again.
            merchant_reference: Optional merchant reference for the refund.
            amount: Optional refund amount. If not provided, a full refund
                is created. For partial refunds, specify the amount to refund.

        Returns:
            The Refund object with refund details.

        Raises:
            PostFinanceError: If the request fails (e.g., transaction not
                in a refundable state, already fully refunded, etc.).
        """
        amount_float = float(amount) if amount is not None else None
        refund_create = RefundCreate(
            transaction=transaction_id,
            externalId=external_id,
            type=RefundType.MERCHANT_INITIATED_ONLINE,
            merchantReference=merchant_reference,
            amount=amount_float,
        )

        try:
            return self._refunds_service.post_payment_refunds(
                space=self.space_id,
                refund_create=refund_create,
            )
        except ApiException as e:
            logger.error("PostFinance API error creating refund: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error creating refund: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_refund(self, refund_id: int) -> Refund:
        """
        Retrieve a refund by its ID.

        Args:
            refund_id: The ID of the refund.

        Returns:
            The Refund object with refund details.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            return self._refunds_service.get_payment_refunds_id(
                id=refund_id,
                space=self.space_id,
            )
        except ApiException as e:
            logger.error("PostFinance API error getting refund: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting refund: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def is_webhook_signature_valid(
        self,
        signature_header: str,
        content: str,
    ) -> bool:
        """
        Validate webhook signature using the SDK's encryption service.

        Uses the X-Signature header and raw request body to verify that
        the webhook payload was actually sent by PostFinance and hasn't
        been tampered with.

        Args:
            signature_header: The value of the X-Signature HTTP header.
            content: The raw request body as a string.

        Returns:
            True if the signature is valid, False otherwise.

        Raises:
            PostFinanceError: If there's an error validating the signature
                (e.g., invalid header format, unknown key ID).
        """
        try:
            result = self._webhook_encryption_service.is_content_valid(
                signature_header=signature_header,
                content_to_verify=content,
            )
            return bool(result)
        except ApiException as e:
            logger.error("PostFinance API error validating webhook signature: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error validating webhook signature: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_webhook_urls(self) -> list[WebhookUrl]:
        """
        Get all webhook URLs configured for this space.

        Returns:
            List of WebhookUrl objects.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            response = self._webhook_url_service.get_webhooks_urls(
                space=self.space_id,
                limit=100,
            )
            return response.data or []
        except ApiException as e:
            logger.error("PostFinance API error getting webhook URLs: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting webhook URLs: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def create_webhook_url(self, name: str, url: str) -> WebhookUrl:
        """
        Create a new webhook URL.

        Args:
            name: A name for this webhook URL configuration.
            url: The URL where webhooks will be sent.

        Returns:
            The created WebhookUrl object.

        Raises:
            PostFinanceError: If the request fails.
        """
        webhook_url_create = WebhookUrlCreate(
            name=name,
            url=url,
            state=CreationEntityState.ACTIVE,
        )

        try:
            return self._webhook_url_service.post_webhooks_urls(
                space=self.space_id,
                webhook_url_create=webhook_url_create,
            )
        except ApiException as e:
            logger.error("PostFinance API error creating webhook URL: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error creating webhook URL: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def get_webhook_listeners(self) -> list[WebhookListener]:
        """
        Get all webhook listeners configured for this space.

        Returns:
            List of WebhookListener objects.

        Raises:
            PostFinanceError: If the request fails.
        """
        try:
            response = self._webhook_listener_service.get_webhooks_listeners(
                space=self.space_id,
                limit=100,
            )
            return response.data or []
        except ApiException as e:
            logger.error("PostFinance API error getting webhook listeners: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error getting webhook listeners: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def create_webhook_listener(
        self,
        name: str,
        webhook_url_id: int,
        entity_id: int,
        entity_states: list[str],
    ) -> WebhookListener:
        """
        Create a new webhook listener.

        Args:
            name: A name for this webhook listener.
            webhook_url_id: The ID of the webhook URL to use.
            entity_id: The entity type ID to listen for.
                Common values: 1472041829003 (Transaction), 1472041816898 (Refund)
            entity_states: List of entity state names to trigger on
                (e.g., ["AUTHORIZED", "COMPLETED"]).

        Returns:
            The created WebhookListener object.

        Raises:
            PostFinanceError: If the request fails.
        """
        webhook_listener_create = WebhookListenerCreate(
            name=name,
            url=webhook_url_id,
            entity=entity_id,
            entityStates=entity_states,
            state=CreationEntityState.ACTIVE,
        )

        try:
            return self._webhook_listener_service.post_webhooks_listeners(
                space=self.space_id,
                webhook_listener_create=webhook_listener_create,
            )
        except ApiException as e:
            logger.error("PostFinance API error creating webhook listener: %s", e)
            raise PostFinanceError(
                message=e.reason or str(e.status),
                status_code=e.status,
                error_code=str(e.status),
            ) from e
        except PostFinanceCheckoutSdkException as e:
            logger.error("PostFinance SDK error creating webhook listener: %s", e)
            raise PostFinanceError(message=str(e)) from e

    def setup_webhooks(self, webhook_url: str) -> dict[str, int | bool | None]:
        """
        Set up webhooks for Transaction and Refund state changes.

        This is a convenience method that:
        1. Creates a webhook URL (or finds an existing one with the same URL)
        2. Creates a webhook listener for Transaction state changes
        3. Creates a webhook listener for Refund state changes

        Args:
            webhook_url: The URL where webhooks will be sent.

        Returns:
            A dict with keys:
            - 'webhook_url_id': ID of the webhook URL
            - 'transaction_listener_id': ID of the transaction listener
            - 'refund_listener_id': ID of the refund listener
            - 'created_transaction_listener': True if created, False if already existed
            - 'created_refund_listener': True if created, False if already existed

        Raises:
            PostFinanceError: If any API request fails.
        """
        # PostFinance entity IDs (these are fixed IDs in PostFinance's system)
        TRANSACTION_ENTITY_ID = 1472041829003
        REFUND_ENTITY_ID = 1472041816898

        # Transaction states we care about (all major state changes)
        TRANSACTION_STATES = [
            TransactionState.AUTHORIZED.value,
            TransactionState.COMPLETED.value,
            TransactionState.FULFILL.value,
            TransactionState.FAILED.value,
            TransactionState.DECLINE.value,
            TransactionState.VOIDED.value,
            TransactionState.CONFIRMED.value,
            TransactionState.PROCESSING.value,
        ]

        # Refund states we care about
        REFUND_STATES = [
            RefundState.SUCCESSFUL.value,
            RefundState.FAILED.value,
        ]

        result: dict[str, int | bool | None] = {
            "webhook_url_id": None,
            "transaction_listener_id": None,
            "refund_listener_id": None,
            "created_transaction_listener": False,
            "created_refund_listener": False,
        }

        # Check if a webhook URL with this URL already exists
        existing_urls = self.get_webhook_urls()
        webhook_url_obj = None
        for existing in existing_urls:
            if existing.url == webhook_url and existing.state == CreationEntityState.ACTIVE:
                webhook_url_obj = existing
                logger.info("Found existing webhook URL with ID %s", existing.id)
                break

        # Create webhook URL if it doesn't exist
        if not webhook_url_obj:
            webhook_url_obj = self.create_webhook_url(
                name="pretix PostFinance Plugin",
                url=webhook_url,
            )
            logger.info("Created webhook URL with ID %s", webhook_url_obj.id)

        if not webhook_url_obj.id:
            raise PostFinanceError("Failed to get webhook URL ID")

        result["webhook_url_id"] = webhook_url_obj.id

        # Check existing listeners to avoid duplicates
        existing_listeners = self.get_webhook_listeners()
        has_transaction_listener = False
        has_refund_listener = False

        for listener in existing_listeners:
            listener_url_id = listener.url.id if listener.url else None
            if listener_url_id != webhook_url_obj.id:
                continue
            if listener.state != CreationEntityState.ACTIVE:
                continue

            if listener.entity == TRANSACTION_ENTITY_ID:
                has_transaction_listener = True
                result["transaction_listener_id"] = listener.id
                logger.info("Found existing transaction listener with ID %s", listener.id)
            elif listener.entity == REFUND_ENTITY_ID:
                has_refund_listener = True
                result["refund_listener_id"] = listener.id
                logger.info("Found existing refund listener with ID %s", listener.id)

        # Create Transaction listener if it doesn't exist
        if not has_transaction_listener:
            transaction_listener = self.create_webhook_listener(
                name="pretix Transaction Updates",
                webhook_url_id=webhook_url_obj.id,
                entity_id=TRANSACTION_ENTITY_ID,
                entity_states=TRANSACTION_STATES,
            )
            result["transaction_listener_id"] = transaction_listener.id
            result["created_transaction_listener"] = True
            logger.info("Created transaction listener with ID %s", transaction_listener.id)

        # Create Refund listener if it doesn't exist
        if not has_refund_listener:
            refund_listener = self.create_webhook_listener(
                name="pretix Refund Updates",
                webhook_url_id=webhook_url_obj.id,
                entity_id=REFUND_ENTITY_ID,
                entity_states=REFUND_STATES,
            )
            result["refund_listener_id"] = refund_listener.id
            result["created_refund_listener"] = True
            logger.info("Created refund listener with ID %s", refund_listener.id)

        return result
