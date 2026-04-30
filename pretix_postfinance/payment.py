from __future__ import annotations

import logging
from collections import OrderedDict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from django import forms
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from postfinancecheckout.models import (
    AddressCreate,
    LineItemCreate,
    LineItemType,
    TransactionState,
)
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri

from .api import PostFinanceClient, PostFinanceError

if TYPE_CHECKING:
    from pretix.base.models import Order

logger = logging.getLogger(__name__)


# PostFinance transaction states that indicate payment is captured/settled
# FULFILL = final state, money confirmed, safe to deliver goods
# COMPLETED = transfer initiated, waiting for final confirmation
# Note: AUTHORIZED means reservation only - funds NOT transferred yet
SUCCESS_STATES = {
    TransactionState.FULFILL,
    TransactionState.COMPLETED,
}

# PostFinance transaction states that indicate failed payment
FAILURE_STATES = {
    TransactionState.FAILED,
    TransactionState.DECLINE,
    TransactionState.VOIDED,
}

# Mapping of HTTP status codes to user-friendly error messages
ERROR_STATUS_MESSAGES = {
    400: _("Bad request. The payment data may be invalid."),
    401: _("Authentication failed. Check your User ID and API Secret in settings."),
    403: _("Access denied. Your API credentials may lack required permissions."),
    404: _("Resource not found. The transaction or space ID may be invalid."),
    409: _("Conflict. The transaction may have already been processed."),
    422: _("Invalid request. Check the payment amount and currency."),
    429: _("Rate limited. Too many requests to PostFinance API."),
    500: _("PostFinance server error. Please try again later."),
    502: _("PostFinance gateway error. Please try again later."),
    503: _("PostFinance service unavailable. Please try again later."),
}

PENDING_TRANSACTION_ID_KEY = "pending_transaction_id"
SESSION_TRANSACTION_ID_KEY = "payment_postfinance_transaction_id"
SESSION_TRANSACTION_PAYMENT_ID_KEY = "payment_postfinance_transaction_payment_id"


class PostFinancePaymentProvider(BasePaymentProvider):
    """
    PostFinance Checkout payment provider for pretix.

    Enables Swiss payment methods including Card, E-Finance, and TWINT
    through the PostFinance Checkout API.
    """

    identifier = "postfinance"
    verbose_name = "PostFinance"
    abort_pending_allowed = False
    execute_payment_needs_user = True

    @property
    def test_mode_message(self) -> str:
        """
        Return a message explaining test mode behavior.

        This is displayed when the payment provider is selected while
        the event is in test mode.
        """
        if self._has_test_credentials():
            return str(
                _(
                    "Test mode is enabled. Payments will use your test credentials "
                    "and no real charges will be made. Configure test credentials "
                    "in the payment settings."
                )
            )
        return str(
            _(
                "Test mode is enabled but no test credentials are configured. "
                "Payments will use your live credentials. Configure separate test "
                "credentials in the payment settings to avoid real charges."
            )
        )

    def _has_test_credentials(self) -> bool:
        """Check if test mode credentials are configured."""
        return bool(
            self.settings.get("test_space_id")
            and self.settings.get("test_user_id")
            and self.settings.get("test_auth_key")
        )

    def _get_credentials_for_mode(
        self, mode: Literal["live", "test"]
    ) -> tuple[str | None, str | None, str | None]:
        """Return credentials for an explicit space (live or test)."""
        if mode == "test":
            return (
                self.settings.get("test_space_id"),
                self.settings.get("test_user_id"),
                self.settings.get("test_auth_key"),
            )
        return (
            self.settings.get("space_id"),
            self.settings.get("user_id"),
            self.settings.get("auth_key"),
        )

    def _get_credentials(self) -> tuple[str | None, str | None, str | None]:
        """
        Get the appropriate credentials based on event test mode.

        Returns test credentials if the event is in test mode and test
        credentials are configured, otherwise returns live credentials.
        """
        if self.event.testmode and self._has_test_credentials():
            return self._get_credentials_for_mode("test")
        return self._get_credentials_for_mode("live")

    @property
    def public_name(self) -> str:
        """
        Return the name shown to customers during checkout.

        If a custom display name is configured in event settings, use that.
        Otherwise fall back to the default verbose name.
        """
        return str(self.settings.get("public_name")) or self.verbose_name

    def _get_payment_method_choices(self) -> list[tuple[str, str]]:
        """
        Fetch available payment method configurations from PostFinance.

        Returns a list of (id, name) tuples for use in a MultipleChoiceField.
        Returns an empty list if credentials are not configured or API call fails.
        """
        space_id, user_id, auth_key = self._get_credentials()

        if not all([space_id, user_id, auth_key]):
            return []

        try:
            client = self._get_client()
            configs = client.get_payment_method_configurations()
            choices = []
            for config in configs:
                if config.id is not None:
                    # Use resolved_title if available, fall back to name
                    name = config.name or str(config.id)
                    if config.resolved_title:
                        # resolved_title is a dict of language -> title
                        # Try to get English or first available
                        title = config.resolved_title.get(
                            "en-US", config.resolved_title.get("en", "")
                        )
                        if title:
                            name = title
                    choices.append((str(config.id), name))
            return sorted(choices, key=lambda x: x[1])
        except Exception:
            # Settings form rendering must never break on bad credentials.
            # The SDK raises non-PostFinanceError exceptions for malformed
            # auth keys (e.g. binascii.Error) before any HTTP call.
            logger.warning("Failed to fetch payment method configurations", exc_info=True)
            return []

    def _parse_allowed_payment_methods(self) -> list[int] | None:
        """
        Parse the allowed_payment_methods setting.

        Returns:
            List of payment method configuration IDs, or None if all methods allowed.
        """
        allowed_methods = self.settings.get("allowed_payment_methods", as_type=list)

        if not allowed_methods:
            return None

        try:
            return [int(x) for x in allowed_methods if x]
        except (ValueError, TypeError):
            logger.warning("Invalid allowed_payment_methods list: %s", allowed_methods)

        return None

    @property
    def settings_form_fields(self) -> OrderedDict:
        """
        Return the form fields for the payment provider settings.

        These will be displayed in the event's payment settings.
        """
        # Get dynamic payment method choices
        payment_method_choices = self._get_payment_method_choices()

        d = OrderedDict(
            list(super().settings_form_fields.items())
            + [
                (
                    "public_name",
                    forms.CharField(
                        label=_("Display name"),
                        help_text=_(
                            "Custom name shown to customers during checkout. "
                            "Leave empty to use the default name 'PostFinance'."
                        ),
                        required=False,
                    ),
                ),
                (
                    "description",
                    forms.CharField(
                        label=_("Description"),
                        help_text=_(
                            "Custom description shown on the checkout page. "
                            "Leave empty to use the default message."
                        ),
                        widget=forms.Textarea(attrs={"rows": 3}),
                        required=False,
                    ),
                ),
                (
                    "space_id",
                    forms.CharField(
                        label=_("Space ID"),
                        help_text=_(
                            "Your PostFinance Checkout space ID. "
                            "You can find it next to your space name in your "
                            "PostFinance Checkout account."
                        ),
                        required=True,
                    ),
                ),
                (
                    "user_id",
                    forms.CharField(
                        label=_("User ID"),
                        help_text=_(
                            "Your PostFinance Checkout application user ID. "
                            "Create an application user in your PostFinance Checkout account "
                            "under Account > Users > Application Users."
                        ),
                        required=True,
                    ),
                ),
                (
                    "auth_key",
                    SecretKeySettingsField(
                        label=_("Authentication key"),
                        help_text=_(
                            "The authentication key for your application user. "
                            "This is shown only once when creating the application user."
                        ),
                        required=True,
                    ),
                ),
                (
                    "test_space_id",
                    forms.CharField(
                        label=_("Test space ID"),
                        help_text=_(
                            "Space ID for test mode. When the event is in test mode "
                            "and this is configured, payments will use test credentials. "
                            "Leave empty to use live credentials in test mode."
                        ),
                        required=False,
                    ),
                ),
                (
                    "test_user_id",
                    forms.CharField(
                        label=_("Test user ID"),
                        help_text=_(
                            "User ID for test mode. Required if Test space ID is set."
                        ),
                        required=False,
                    ),
                ),
                (
                    "test_auth_key",
                    SecretKeySettingsField(
                        label=_("Test authentication key"),
                        help_text=_(
                            "Authentication key for test mode. Required if Test space ID is set."
                        ),
                        required=False,
                    ),
                ),
                (
                    "allowed_payment_methods",
                    forms.MultipleChoiceField(
                        label=_("Allowed payment methods"),
                        help_text=_(
                            "Select which payment methods are available to customers. "
                            "Leave empty to allow all payment methods. "
                            "Save your credentials first to see available options."
                        ),
                        choices=payment_method_choices,
                        widget=forms.CheckboxSelectMultiple,
                        required=False,
                    ),
                ),
            ]
        )
        return d

    def settings_form_clean(self, cleaned_data: dict) -> dict:
        if cleaned_data.get("_enabled"):
            missing: list = []
            if not cleaned_data.get("space_id"):
                missing.append(str(_("Space ID")))
            if not cleaned_data.get("user_id"):
                missing.append(str(_("User ID")))
            if not cleaned_data.get("auth_key"):
                missing.append(str(_("Authentication key")))

            if cleaned_data.get("test_space_id"):
                if not cleaned_data.get("test_user_id"):
                    missing.append(str(_("Test user ID")))
                if not cleaned_data.get("test_auth_key"):
                    missing.append(str(_("Test authentication key")))

            if missing:
                msg = _(
                    "The following fields are required to enable "
                    "this payment provider: {fields}"
                ).format(fields=", ".join(missing))
                raise ValidationError(msg)

        return cleaned_data

    def settings_content_render(self, request: HttpRequest) -> str:
        """
        Render additional content below the settings form.

        Shows webhook URL and adds a "Test connection" button that validates
        the configured PostFinance credentials via AJAX, and a "Setup webhooks"
        button to automatically configure webhooks in PostFinance.
        """
        template = get_template("pretixplugins/postfinance/control_settings.html")
        ctx = {
            "request": request,
            "webhook_url": build_global_uri(
                "plugins:pretix_postfinance:postfinance.webhook",
            ),
            "test_url": reverse(
                "plugins:pretix_postfinance:postfinance.test_connection",
                kwargs={
                    "organizer": self.event.organizer.slug,
                    "event": self.event.slug,
                },
            ),
            "setup_webhooks_url": reverse(
                "plugins:pretix_postfinance:postfinance.setup_webhooks",
                kwargs={
                    "organizer": self.event.organizer.slug,
                    "event": self.event.slug,
                },
            ),
        }
        return template.render(ctx)

    def _get_client_for_mode(self, mode: Literal["live", "test"]) -> PostFinanceClient:
        """Create a PostFinance API client for an explicit space (live or test)."""
        space_id, user_id, auth_key = self._get_credentials_for_mode(mode)

        logger.debug(
            "Creating PostFinance client for event %s: space_id=%s, user_id=%s, "
            "auth_key=%s, mode=%s",
            self.event.slug,
            space_id,
            user_id,
            "***" if auth_key else "(empty)",
            mode,
        )

        return PostFinanceClient(
            space_id=int(space_id) if space_id else 0,
            user_id=int(user_id) if user_id else 0,
            api_secret=str(auth_key) if auth_key else "",
        )

    def _get_client(self) -> PostFinanceClient:
        """
        Create a client using the credentials picked by `event.testmode`.

        Used at runtime during payments — admin actions should call
        `_get_client_for_mode()` directly with an explicit mode.
        """
        mode: Literal["live", "test"] = (
            "test" if self.event.testmode and self._has_test_credentials() else "live"
        )
        return self._get_client_for_mode(mode)

    def test_connection(
        self, mode: Literal["live", "test"] | None = None
    ) -> tuple[bool, str]:
        """
        Test the connection to PostFinance API for a specific space.

        If `mode` is None, defaults to test credentials when the event is in
        test mode and test credentials are configured, otherwise live.

        Returns:
            A tuple of (success: bool, message: str).
        """
        if mode is None:
            mode = "test" if self.event.testmode and self._has_test_credentials() else "live"

        space_id, user_id, auth_key = self._get_credentials_for_mode(mode)

        if not all([space_id, user_id, auth_key]):
            if mode == "test":
                return (
                    False,
                    str(
                        _(
                            "Please configure test space ID, user ID, and authentication key "
                            "before testing the connection."
                        )
                    ),
                )
            return (
                False,
                str(
                    _(
                        "Please configure Space ID, user ID, and authentication key before "
                        "testing the connection."
                    )
                ),
            )

        try:
            client = self._get_client_for_mode(mode)
            space = client.get_space()
            space_name = space.name if space.name else str(_("Unknown"))
            mode_label = str(_("test")) if mode == "test" else str(_("live"))
            return (
                True,
                str(
                    _(
                        "Connection successful! Connected to {mode} space: {space_name}"
                    ).format(mode=mode_label, space_name=space_name)
                ),
            )
        except PostFinanceError as e:
            if e.status_code == 401:
                return (
                    False,
                    str(_("Authentication failed. Please check your User ID and API Secret.")),
                )
            elif e.status_code == 404:
                return (
                    False,
                    str(_("Space not found. Please check your Space ID.")),
                )
            return (False, str(_("Connection failed: {error}").format(error=str(e))))
        except Exception as e:
            return (False, str(_("Unexpected error: {error}").format(error=str(e))))

    def setup_webhooks(
        self, webhook_url: str, mode: Literal["live", "test"]
    ) -> tuple[bool, str]:
        """
        Create the PostFinance webhook URL and listeners for a specific space.

        Returns:
            A tuple of (success: bool, message: str).
        """
        space_id, user_id, auth_key = self._get_credentials_for_mode(mode)

        if not all([space_id, user_id, auth_key]):
            if mode == "test":
                return (
                    False,
                    str(
                        _(
                            "Please configure test space ID, user ID, and authentication key "
                            "before setting up webhooks."
                        )
                    ),
                )
            return (
                False,
                str(
                    _(
                        "Please configure Space ID, user ID, and authentication key before "
                        "setting up webhooks."
                    )
                ),
            )

        try:
            client = self._get_client_for_mode(mode)
            result = client.setup_webhooks(webhook_url)
        except PostFinanceError as e:
            return (
                False,
                str(_("Failed to setup webhooks: {error}").format(error=str(e))),
            )
        except Exception as e:
            logger.warning("Unexpected error setting up webhooks", exc_info=True)
            return (
                False,
                str(_("Failed to setup webhooks: {error}").format(error=str(e))),
            )

        created_transaction = result.get("created_transaction_listener", False)
        created_refund = result.get("created_refund_listener", False)

        if created_transaction and created_refund:
            message = _(
                "Webhooks configured successfully! "
                "Transaction and refund updates will be received automatically."
            )
        elif created_transaction:
            message = _(
                "Transaction webhook configured. Refund webhook was already set up."
            )
        elif created_refund:
            message = _(
                "Refund webhook configured. Transaction webhook was already set up."
            )
        else:
            message = _("Webhooks are already configured. No changes were needed.")

        return (True, str(message))

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        """
        Allow pretix to reach review and payment execution steps.

        PostFinance transactions are created once pretix has an OrderPayment and
        can generate callback URLs that point to that order.
        """
        return True

    def checkout_prepare(self, request: HttpRequest, cart: dict[str, Any]) -> bool | str:
        """
        Clear any stale transaction state before pretix creates a new OrderPayment.
        """
        self._clear_session_transaction_id(request)
        return super().checkout_prepare(request, cart)

    def _get_request_payment(self, request: HttpRequest) -> OrderPayment | None:
        resolver_match = getattr(request, "resolver_match", None)
        kwargs = getattr(resolver_match, "kwargs", None) or {}
        payment_id = kwargs.get("payment")

        if payment_id is None:
            return None

        return OrderPayment.objects.filter(
            pk=payment_id,
            provider=self.identifier,
            order__event=self.event,
        ).only("info").first()

    def _get_payment_transaction_id(self, payment: OrderPayment | None) -> int | None:
        if payment is None:
            return None

        raw_value = (
            payment.info_data.get(PENDING_TRANSACTION_ID_KEY)
            or payment.info_data.get("transaction_id")
        )
        if raw_value is None:
            return None

        try:
            return int(raw_value)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid PostFinance transaction ID %r stored on payment %s",
                raw_value,
                payment.pk,
            )
            return None

    def _get_prepared_transaction_id(
        self,
        request: HttpRequest,
        payment: OrderPayment | None = None,
    ) -> int | None:
        if payment is not None:
            if payment_transaction_id := self._get_payment_transaction_id(payment):
                return payment_transaction_id

            session_transaction_id = request.session.get(SESSION_TRANSACTION_ID_KEY)
            session_payment_id = request.session.get(SESSION_TRANSACTION_PAYMENT_ID_KEY)
            if session_transaction_id is not None and session_payment_id == payment.pk:
                return int(session_transaction_id)

            return None

        session_transaction_id = request.session.get(SESSION_TRANSACTION_ID_KEY)
        if session_transaction_id is not None:
            return int(session_transaction_id)

        return self._get_payment_transaction_id(payment or self._get_request_payment(request))

    def _set_session_transaction_id(
        self, request: HttpRequest, payment: OrderPayment, transaction_id: int
    ) -> None:
        request.session[SESSION_TRANSACTION_ID_KEY] = transaction_id
        request.session[SESSION_TRANSACTION_PAYMENT_ID_KEY] = payment.pk

    def _clear_session_transaction_id(self, request: HttpRequest) -> None:
        request.session.pop(SESSION_TRANSACTION_ID_KEY, None)
        request.session.pop(SESSION_TRANSACTION_PAYMENT_ID_KEY, None)

    def _set_pending_transaction_id(self, payment: OrderPayment, transaction_id: int) -> None:
        info = payment.info_data
        info[PENDING_TRANSACTION_ID_KEY] = transaction_id
        payment.info_data = info
        payment.save(update_fields=["info"])

    def _clear_pending_transaction_id(self, payment: OrderPayment) -> None:
        info = payment.info_data
        if info.pop(PENDING_TRANSACTION_ID_KEY, None) is None:
            return

        payment.info_data = info
        payment.save(update_fields=["info"])

    def _build_payment_transaction_line_items(
        self, payment: OrderPayment, detailed_line_items: bool = False
    ) -> list[LineItemCreate]:
        if detailed_line_items and payment.amount == payment.order.total:
            return self._build_line_items(
                {
                    "positions": payment.order.positions.filter(canceled=False).select_related(
                        "item", "variation"
                    ),
                    "fees": payment.order.fees.filter(canceled=False),
                    "total": payment.amount,
                },
                payment.order.event.currency,
            )

        return [
            LineItemCreate(
                name=str(_("Payment for order {code}")).format(code=payment.order.code),
                quantity=1,
                amountIncludingTax=float(payment.amount),
                type=LineItemType.PRODUCT,
                uniqueId=f"payment-{payment.pk}",
            )
        ]

    def _build_transaction_billing_address(
        self, payment: OrderPayment
    ) -> AddressCreate | None:
        try:
            invoice_address = payment.order.invoice_address
        except ObjectDoesNotExist:
            return None

        name_parts = invoice_address.name_parts or {}
        given_name = name_parts.get("given_name") or None
        family_name = name_parts.get("family_name") or None

        if not given_name and not family_name:
            # Pretix can store the customer name as a single field.
            full_name = str(
                name_parts.get("full_name") or invoice_address.name_cached or ""
            ).strip()
            if full_name:
                given_name = full_name

        billing_address = AddressCreate(
            given_name=given_name,
            family_name=family_name,
            salutation=name_parts.get("salutation") or None,
            organization_name=invoice_address.company or None,
            street=invoice_address.street or None,
            postcode=invoice_address.zipcode or None,
            city=invoice_address.city or None,
            country=str(invoice_address.country) or None,
            postal_state=invoice_address.state_for_address or invoice_address.state or None,
            sales_tax_number=invoice_address.vat_id or None,
            email_address=payment.order.email or None,
            phone_number=str(payment.order.phone) if payment.order.phone else None,
        )

        if not any(
            [
                billing_address.given_name,
                billing_address.family_name,
                billing_address.salutation,
                billing_address.organization_name,
                billing_address.street,
                billing_address.postcode,
                billing_address.city,
                billing_address.country,
                billing_address.postal_state,
                billing_address.sales_tax_number,
                billing_address.email_address,
                billing_address.phone_number,
            ]
        ):
            return None

        return billing_address

    def _create_payment_transaction(
        self, payment: OrderPayment, detailed_line_items: bool = False
    ) -> tuple[int, str]:
        client = self._get_client()
        line_items = self._build_payment_transaction_line_items(
            payment, detailed_line_items=detailed_line_items
        )

        success_url = build_absolute_uri(
            self.event,
            "presale:event.order.pay.complete",
            kwargs={
                "order": payment.order.code,
                "secret": payment.order.secret,
                "payment": payment.pk,
            },
        )
        failed_url = build_absolute_uri(
            self.event,
            "presale:event.order.pay",
            kwargs={
                "order": payment.order.code,
                "secret": payment.order.secret,
                "payment": payment.pk,
            },
        )

        transaction = client.create_transaction(
            currency=payment.order.event.currency,
            line_items=line_items,
            success_url=success_url,
            failed_url=failed_url,
            merchant_reference=f"{self.event.slug}-{payment.order.code}",
            allowed_payment_method_configurations=self._parse_allowed_payment_methods(),
            customer_email_address=payment.order.email,
            billing_address=self._build_transaction_billing_address(payment),
        )

        transaction_id = transaction.id
        if not transaction_id:
            logger.error("PostFinance transaction missing ID for payment %s", payment.pk)
            raise PaymentException(str(_("Failed to create payment. Please try again.")))

        payment_page_url = client.get_payment_page_url(transaction_id)
        if not payment_page_url:
            logger.error(
                "Failed to get payment page URL for transaction %s",
                transaction_id,
            )
            raise PaymentException(
                str(_("Failed to redirect to payment page. Please try again."))
            )

        return transaction_id, payment_page_url

    def _build_line_items(self, cart: dict[str, Any], currency: str) -> list[LineItemCreate]:
        """
        Build PostFinance line items from pretix cart.

        Creates detailed line items for each cart position and fee.
        """
        line_items: list[LineItemCreate] = []

        # Add individual items from grouped positions
        positions = cart.get("positions", [])
        for idx, position in enumerate(positions):
            # Get item name, including variation if applicable
            item_name = str(position.item.name)
            if hasattr(position, "variation") and position.variation:
                item_name = f"{item_name} - {position.variation.value}"

            # Get quantity (grouped positions have a count attribute)
            quantity = getattr(position, "count", 1)

            # Get the total price for this position (includes quantity)
            price = getattr(position, "total", getattr(position, "price", Decimal("0")))

            line_items.append(
                LineItemCreate(
                    name=item_name,
                    quantity=float(quantity),
                    amountIncludingTax=float(price),
                    type=LineItemType.PRODUCT,
                    uniqueId=f"position-{idx}-{position.item.pk}",
                )
            )

        # Add fees (surcharges, taxes, etc.)
        fees = cart.get("fees", [])
        for idx, fee in enumerate(fees):
            fee_value = getattr(fee, "value", Decimal("0"))
            if fee_value == Decimal("0"):
                continue

            # Get fee description
            fee_name = str(_("Fee"))
            fee_description = getattr(fee, "description", None)
            if isinstance(fee_description, str) and fee_description:
                fee_name = fee_description
            elif hasattr(fee, "get_fee_type_display"):
                fee_name = str(fee.get_fee_type_display())
            elif hasattr(fee, "fee_type"):
                fee_name = str(fee.fee_type)

            line_items.append(
                LineItemCreate(
                    name=fee_name,
                    quantity=1,
                    amountIncludingTax=float(fee_value),
                    type=LineItemType.FEE,
                    uniqueId=f"fee-{idx}",
                )
            )

        # Fallback: if no positions were found, use total as single line item
        if not line_items:
            total = cart.get("total", Decimal("0"))
            line_items.append(
                LineItemCreate(
                    name=str(_("Order Total")),
                    quantity=1,
                    amountIncludingTax=float(total),
                    type=LineItemType.PRODUCT,
                    uniqueId="order-total",
                )
            )

        return line_items

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment) -> bool | str:
        """
        Prepare a payment retry or payment method update for an existing order.

        Creates a PostFinance transaction for the existing payment object and
        returns the hosted payment page URL.
        """
        # Validate form
        if not super().payment_prepare(request, payment):
            return False

        self._clear_session_transaction_id(request)
        self._clear_pending_transaction_id(payment)

        try:
            transaction_id, payment_page_url = self._create_payment_transaction(payment)
            self._set_session_transaction_id(request, payment, transaction_id)
            self._set_pending_transaction_id(payment, transaction_id)
            logger.info(
                "Created PostFinance transaction %s for payment %s",
                transaction_id,
                payment.pk,
            )
            return payment_page_url

        except PaymentException as e:
            self._clear_session_transaction_id(request)
            self._clear_pending_transaction_id(payment)
            messages.error(request, str(e))
            return False
        except PostFinanceError as e:
            logger.exception("PostFinance API error during payment_prepare: %s", e)
            self._clear_session_transaction_id(request)
            self._clear_pending_transaction_id(payment)
            messages.error(
                request,
                str(_("Payment service error. Please try again later.")),
            )
            return False
        except Exception as e:
            logger.exception("Unexpected error during payment_prepare: %s", e)
            self._clear_session_transaction_id(request)
            self._clear_pending_transaction_id(payment)
            messages.error(
                request,
                str(_("An unexpected error occurred. Please try again.")),
            )
            return False

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order | None = None, info_data: dict | None = None
    ) -> str:
        """
        Render the payment confirmation page content.

        This is displayed to the customer before they confirm their order
        to summarize what will happen during payment.
        """
        template = get_template("pretixplugins/postfinance/checkout_payment_confirm.html")
        ctx = {
            "request": request,
            "event": self.event,
            "provider": self,
            "description": self.settings.get("description"),
        }
        return template.render(ctx)

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str | None:
        """
        Execute the payment after the order is confirmed.

        Retrieves the transaction details from PostFinance, checks the
        transaction state, and confirms or fails the payment accordingly.
        """
        transaction_id = self._get_prepared_transaction_id(request, payment)

        if not transaction_id:
            try:
                transaction_id, payment_page_url = self._create_payment_transaction(
                    payment,
                    detailed_line_items=request.method == "GET",
                )
                self._set_pending_transaction_id(payment, transaction_id)
                logger.info(
                    "Created PostFinance transaction %s for payment %s during execute_payment",
                    transaction_id,
                    payment.pk,
                )
                return payment_page_url
            except PostFinanceError as e:
                logger.exception(
                    "PostFinance API error during execute_payment transaction creation: %s",
                    e,
                )
                payment.info_data = {
                    "error": str(e),
                    "error_code": e.error_code,
                    "error_status_code": e.status_code,
                }
                payment.save(update_fields=["info"])
                user_message = _("Payment processing failed. Please try again.")
                if e.status_code and e.status_code in ERROR_STATUS_MESSAGES:
                    user_message = ERROR_STATUS_MESSAGES[e.status_code]
                raise PaymentException(str(user_message)) from e
            except PaymentException as e:
                payment.info_data = {"error": str(e)}
                payment.save(update_fields=["info"])
                raise
            except Exception as e:
                logger.exception(
                    "Unexpected error during execute_payment transaction creation: %s",
                    e,
                )
                payment.info_data = {
                    "error": str(e),
                    "error_code": type(e).__name__,
                }
                payment.save(update_fields=["info"])
                raise PaymentException(
                    str(_("An unexpected error occurred. Please try again."))
                ) from e

        try:
            client = self._get_client()
            transaction = client.get_transaction(transaction_id)

            payment_method = None
            if transaction.payment_connector_configuration:
                payment_method = transaction.payment_connector_configuration.name

            state = transaction.state
            payment.info_data = {
                "transaction_id": transaction_id,
                "state": state.value if state else None,
                "payment_method": payment_method,
                "created_on": str(transaction.created_on) if transaction.created_on else None,
            }
            payment.save(update_fields=["info"])

            logger.info(
                "PostFinance transaction %s has state %s for payment %s",
                transaction_id,
                state,
                payment.pk,
            )

            if state in SUCCESS_STATES:
                # Check if already confirmed (webhook may have processed first)
                payment.refresh_from_db()
                if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
                    logger.info(
                        "Payment %s already confirmed, skipping (PostFinance state: %s)",
                        payment.pk,
                        state,
                    )
                else:
                    try:
                        payment.confirm()
                        logger.info(
                            "Payment %s confirmed (PostFinance state: %s)",
                            payment.pk,
                            state,
                        )
                    except Exception as e:
                        logger.exception(
                            "Error confirming payment %s: %s",
                            payment.pk,
                            e,
                        )
                        raise PaymentException(
                            str(_("Payment was successful but order confirmation failed."))
                        ) from e
            elif state in FAILURE_STATES:
                payment.fail(info={"state": state.value if state else None})
                logger.info(
                    "Payment %s failed (PostFinance state: %s)",
                    payment.pk,
                    state,
                )
            else:
                logger.info(
                    "Payment %s is pending (PostFinance state: %s)",
                    payment.pk,
                    state,
                )

        except PostFinanceError as e:
            logger.exception("PostFinance API error during execute_payment: %s", e)
            payment.info_data = {
                "transaction_id": transaction_id,
                "error": str(e),
                "error_code": e.error_code,
                "error_status_code": e.status_code,
            }
            payment.save(update_fields=["info"])
            user_message = _("Payment processing failed. Please try again.")
            if e.status_code and e.status_code in ERROR_STATUS_MESSAGES:
                user_message = ERROR_STATUS_MESSAGES[e.status_code]
            raise PaymentException(str(user_message)) from e

        except Exception as e:
            logger.exception("Unexpected error during execute_payment: %s", e)
            payment.info_data = {
                "transaction_id": transaction_id,
                "error": str(e),
                "error_code": type(e).__name__,
            }
            payment.save(update_fields=["info"])
            raise PaymentException(
                str(_("An unexpected error occurred. Please try again."))
            ) from e

        finally:
            # Always clean up session, whether success or failure
            self._clear_session_transaction_id(request)

        return None

    def payment_pending_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        """
        Render customer-facing instructions on how to proceed with a pending payment.
        """
        info_data = payment.info_data or {}
        template = get_template("pretixplugins/postfinance/pending.html")
        ctx = {
            "request": request,
            "event": self.event,
            "order": payment.order,
            "payment": payment,
            "payment_info": info_data,
            "transaction_id": info_data.get("transaction_id"),
            "state": info_data.get("state"),
        }
        return template.render(ctx)

    def payment_control_render(self, request: HttpRequest, payment: OrderPayment) -> str:
        """
        Render payment control HTML for the admin order view.

        Displays PostFinance transaction details and action buttons.
        """
        info_data = payment.info_data or {}
        transaction_id = info_data.get("transaction_id")

        # Build dashboard URL if we have the required info
        dashboard_url = None
        space_id = self.settings.get("space_id")
        if transaction_id and space_id:
            dashboard_url = (
                f"https://checkout.postfinance.ch/s/{space_id}"
                f"/payment/transaction/view/{transaction_id}"
            )

        # Get error suggestion if applicable
        error_suggestion = None
        error_status = info_data.get("error_status_code")
        if error_status and int(error_status) in ERROR_STATUS_MESSAGES:
            error_suggestion = ERROR_STATUS_MESSAGES[int(error_status)]

        template = get_template("pretixplugins/postfinance/control.html")
        ctx = {
            "request": request,
            "event": self.event,
            "payment": payment,
            "payment_info": info_data,
            "dashboard_url": dashboard_url,
            "error_suggestion": error_suggestion,
        }
        return template.render(ctx)

    def payment_presale_render(self, payment: OrderPayment) -> str:
        """
        Return a short description of the payment for customer view.
        """
        info_data = payment.info_data or {}
        payment_method = info_data.get("payment_method")
        if payment_method:
            return f"{self.public_name} ({payment_method})"
        return str(self.public_name)

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        """
        Return a very short version of the payment method for admin actions.
        """
        info_data = payment.info_data or {}
        transaction_id = info_data.get("transaction_id")
        if transaction_id:
            return f"PostFinance ({transaction_id})"
        return "PostFinance"

    def payment_refund_supported(self, payment: OrderPayment) -> bool:
        """
        Check if automatic refunding is supported for this payment.
        """
        info_data = payment.info_data or {}
        state = info_data.get("state")
        refundable_states = {
            TransactionState.COMPLETED.value,
            TransactionState.FULFILL.value,
        }
        return state in refundable_states

    def payment_partial_refund_supported(self, payment: OrderPayment) -> bool:
        """
        Check if automatic partial refunding is supported for this payment.
        """
        return self.payment_refund_supported(payment)

    def execute_refund(self, refund: OrderRefund, user: str = "system") -> None:
        """
        Execute a refund for an order.

        This is called by pretix when a refund needs to be processed.

        Args:
            refund: The OrderRefund to process.
            user: The user performing the action (for audit logging).
        """
        payment = refund.payment
        info_data = payment.info_data or {}
        transaction_id = info_data.get("transaction_id")

        if not transaction_id:
            raise PaymentException(_("Transaction ID not found."))

        # Check if transaction is in a refundable state
        current_state = info_data.get("state")
        refundable_states = {
            TransactionState.COMPLETED.value,
            TransactionState.FULFILL.value,
        }
        if current_state not in refundable_states:
            raise PaymentException(
                _("Transaction cannot be refunded. Current state: {state}").format(
                    state=current_state or "Unknown"
                )
            )

        try:
            client = self._get_client()

            # Generate a unique external ID for idempotency
            external_id = f"pretix-{self.event.slug}-{refund.order.code}-R-{refund.local_id}"
            merchant_reference = f"{self.event.slug}-{refund.order.code}"

            postfinance_refund = client.refund_transaction(
                transaction_id=int(transaction_id),
                external_id=external_id,
                merchant_reference=merchant_reference,
                amount=refund.amount,
            )

            # Store refund info on the OrderRefund object
            refund.info_data = {
                "refund_id": postfinance_refund.id,
                "state": postfinance_refund.state.value if postfinance_refund.state else None,
                "amount": float(postfinance_refund.amount) if postfinance_refund.amount else None,
                "created_on": str(postfinance_refund.created_on)
                if postfinance_refund.created_on
                else None,
            }
            cast(Any, refund).state = OrderRefund.REFUND_STATE_TRANSIT
            refund.save(update_fields=["info", "state"])

            logger.info(
                "PostFinance refund %s created for payment %s (amount %s)",
                postfinance_refund.id,
                payment.pk,
                refund.amount,
            )

            # Audit log for successful refund
            refund.order.log_action(
                "pretix_postfinance.refund",
                data={
                    "transaction_id": transaction_id,
                    "refund_id": postfinance_refund.id,
                    "amount": str(refund.amount),
                    "user": user,
                    "success": True,
                },
            )

        except PostFinanceError as e:
            logger.exception(
                "PostFinance API error refunding transaction %s: %s",
                transaction_id,
                e,
            )
            # Store error details in refund.info for admin visibility
            refund_info_data = refund.info_data or {}
            refund_info_data.update(
                {
                    "error": str(e),
                    "error_code": e.error_code,
                    "error_status_code": e.status_code,
                }
            )
            refund.info_data = refund_info_data
            refund.save(update_fields=["info"])

            # Audit log for failed refund
            refund.order.log_action(
                "pretix_postfinance.refund.failed",
                data={
                    "transaction_id": transaction_id,
                    "amount": str(refund.amount),
                    "user": user,
                    "success": False,
                    "error": str(e),
                },
            )
            user_message = _("Refund failed. Please try again.")
            if e.status_code and e.status_code in ERROR_STATUS_MESSAGES:
                user_message = ERROR_STATUS_MESSAGES[e.status_code]
            raise PaymentException(str(user_message)) from e

    def api_payment_details(self, payment: OrderPayment) -> dict:
        """
        Return payment details for the REST API.
        """
        info_data = payment.info_data or {}
        return {
            "transaction_id": info_data.get("transaction_id"),
            "state": info_data.get("state"),
            "payment_method": info_data.get("payment_method"),
            "created_on": info_data.get("created_on"),
        }

    def api_refund_details(self, refund: OrderRefund) -> dict:
        """
        Return refund details for the REST API.
        """
        info_data = refund.info_data or {}
        result = {
            "refund_id": info_data.get("refund_id"),
            "state": info_data.get("state"),
            "amount": info_data.get("amount"),
            "created_on": info_data.get("created_on"),
        }
        # Include error fields if present
        if info_data.get("error"):
            result["error"] = info_data.get("error")
            result["error_code"] = info_data.get("error_code")
            result["error_status_code"] = info_data.get("error_status_code")
        return result

    def matching_id(self, payment: OrderPayment) -> str | None:
        """
        Return the transaction ID for matching with external records.
        """
        info_data = payment.info_data or {}
        return info_data.get("transaction_id")

    def refund_matching_id(self, refund: OrderRefund) -> str | None:
        """
        Return the refund ID for matching with external records.
        """
        info_data = refund.info_data or {}
        refund_id = info_data.get("refund_id")
        return str(refund_id) if refund_id else None

    def refund_control_render_short(self, refund: OrderRefund) -> str:
        """
        Return a very short version of the refund method for admin lists.
        """
        info_data = refund.info_data or {}
        refund_id = info_data.get("refund_id")
        if refund_id:
            return f"PostFinance ({refund_id})"
        return "PostFinance"

    def shred_payment_info(self, obj: OrderPayment | OrderRefund) -> None:
        """
        Remove personal data from payment/refund info when requested.
        """
        if not isinstance(obj, (OrderPayment, OrderRefund)):
            return

        # Keep transaction/refund IDs for reference, but remove other details
        if isinstance(obj, OrderPayment):
            info_data = obj.info_data or {}
            obj.info_data = {
                "transaction_id": info_data.get("transaction_id"),
                "state": info_data.get("state"),
                "_shredded": True,
            }
            obj.save(update_fields=["info"])
        else:
            # For refunds, clear the info
            obj.info_data = {"_shredded": True}
            obj.save(update_fields=["info"])
