from __future__ import annotations

import json
import logging
from collections import OrderedDict
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib import messages
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from postfinancecheckout.models import (
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
        space_id = self.settings.get("space_id")
        user_id = self.settings.get("user_id")
        auth_key = self.settings.get("auth_key")

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
        except PostFinanceError:
            logger.warning("Failed to fetch payment method configurations", exc_info=True)
            return []

    def _parse_allowed_payment_methods(self) -> list[int] | None:
        """
        Parse the allowed_payment_methods setting.

        Handles both the new list format (from MultipleChoiceField) and the
        legacy comma-separated string format for backwards compatibility.

        Returns:
            List of payment method configuration IDs, or None if all methods allowed.
        """
        allowed_methods = self.settings.get("allowed_payment_methods")

        if not allowed_methods:
            return None

        # Handle list format (from MultipleChoiceField)
        if isinstance(allowed_methods, list):
            try:
                return [int(x) for x in allowed_methods if x]
            except (ValueError, TypeError):
                logger.warning("Invalid allowed_payment_methods list: %s", allowed_methods)
                return None

        # Handle legacy comma-separated string format
        if isinstance(allowed_methods, str):
            if not allowed_methods.strip():
                return None
            try:
                return [int(x.strip()) for x in allowed_methods.split(",") if x.strip()]
            except ValueError:
                logger.warning("Invalid allowed_payment_methods string: %s", allowed_methods)
                return None

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
                    "public_name",
                    forms.CharField(
                        label=_("Display Name"),
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
                    "allowed_payment_methods",
                    forms.MultipleChoiceField(
                        label=_("Allowed Payment Methods"),
                        help_text=_(
                            "Select which payment methods are available to customers. "
                            "Leave empty to allow all payment methods. "
                            "Save your credentials first to see available options."
                        ),
                        choices=payment_method_choices,
                        widget=forms.CheckboxSelectMultiple,
                        required=False,
                    )
                    if payment_method_choices
                    else forms.CharField(
                        label=_("Allowed Payment Methods"),
                        help_text=_(
                            "Save your Space ID, User ID, and Authentication key first, "
                            "then this field will show available payment methods as checkboxes."
                        ),
                        required=False,
                        widget=forms.TextInput(
                            attrs={"placeholder": _("Configure credentials first")}
                        ),
                    ),
                ),
            ]
        )
        return d

    def settings_content_render(self, request: HttpRequest) -> str:
        """
        Render additional content below the settings form.

        Shows webhook URL and adds a "Test Connection" button that validates
        the configured PostFinance credentials via AJAX, and a "Setup Webhooks"
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

    def _get_client(self) -> PostFinanceClient:
        """
        Create and return a PostFinance API client using the configured settings.
        """
        space_id = self.settings.get("space_id")
        user_id = self.settings.get("user_id")
        auth_key = self.settings.get("auth_key")

        logger.debug(
            "Creating PostFinance client for event %s: space_id=%s, user_id=%s, auth_key=%s",
            self.event.slug,
            space_id,
            user_id,
            "***" if auth_key else "(empty)",
        )

        return PostFinanceClient(
            space_id=int(space_id) if space_id else 0,
            user_id=int(user_id) if user_id else 0,
            api_secret=str(auth_key) if auth_key else "",
        )

    def test_connection(self) -> tuple[bool, str]:
        """
        Test the connection to PostFinance API using configured credentials.

        Returns:
            A tuple of (success: bool, message: str).
        """
        space_id = self.settings.get("space_id")
        user_id = self.settings.get("user_id")
        auth_key = self.settings.get("auth_key")

        if not all([space_id, user_id, auth_key]):
            return (
                False,
                str(
                    _(
                        "Please configure Space ID, User ID, and Authentication Key before "
                        "testing the connection."
                    )
                ),
            )

        try:
            client = self._get_client()
            space = client.get_space()
            space_name = space.name if space.name else str(_("Unknown"))
            return (
                True,
                str(
                    _("Connection successful! Connected to space: {space_name}").format(
                        space_name=space_name
                    )
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

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        """
        Check if the user session contains valid payment information.

        For PostFinance, we need a transaction ID in the session that was
        created during checkout_prepare.
        """
        return request.session.get("payment_postfinance_transaction_id") is not None

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
            if hasattr(fee, "get_fee_type_display"):
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

    def checkout_prepare(self, request: HttpRequest, cart: dict[str, Any]) -> bool | str:
        """
        Prepare the checkout for payment.

        Creates a PostFinance transaction and returns the payment page URL
        to redirect the customer to PostFinance for payment.
        """
        # Fresh start - clear any stale transaction ID from previous attempts
        request.session.pop("payment_postfinance_transaction_id", None)

        try:
            client = self._get_client()
            currency = self.event.currency

            line_items = self._build_line_items(cart, currency)

            success_url = build_absolute_uri(
                self.event,
                "presale:event.checkout",
                kwargs={"step": "confirm"},
            )
            failed_url = build_absolute_uri(
                self.event,
                "presale:event.checkout",
                kwargs={"step": "payment"},
            )

            merchant_reference = f"pretix-{self.event.slug}"

            # Parse allowed payment method configurations
            allowed_payment_methods = self._parse_allowed_payment_methods()

            transaction = client.create_transaction(
                currency=currency,
                line_items=line_items,
                success_url=success_url,
                failed_url=failed_url,
                merchant_reference=merchant_reference,
                allowed_payment_method_configurations=allowed_payment_methods,
            )

            transaction_id = transaction.id
            if not transaction_id:
                logger.error("PostFinance transaction missing ID: %s", transaction)
                messages.error(
                    request,
                    str(_("Failed to create payment. Please try again.")),
                )
                return False

            request.session["payment_postfinance_transaction_id"] = transaction_id
            logger.info(
                "Created PostFinance transaction %s for event %s",
                transaction_id,
                self.event.slug,
            )

            payment_page_url = client.get_payment_page_url(transaction_id)
            if not payment_page_url:
                logger.error(
                    "Failed to get payment page URL for transaction %s",
                    transaction_id,
                )
                request.session.pop("payment_postfinance_transaction_id", None)
                messages.error(
                    request,
                    str(_("Failed to redirect to payment page. Please try again.")),
                )
                return False

            return payment_page_url

        except PostFinanceError as e:
            logger.exception("PostFinance API error during checkout_prepare: %s", e)
            request.session.pop("payment_postfinance_transaction_id", None)
            messages.error(
                request,
                str(_("Payment service error. Please try again later.")),
            )
            return False
        except Exception as e:
            logger.exception("Unexpected error during checkout_prepare: %s", e)
            request.session.pop("payment_postfinance_transaction_id", None)
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
        transaction_id = request.session.get("payment_postfinance_transaction_id")

        if not transaction_id:
            logger.warning(
                "No PostFinance transaction ID in session for payment %s",
                payment.pk,
            )
            payment.info_data = {"error": "No transaction ID in session"}
            payment.save(update_fields=["info"])
            return None

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
            request.session.pop("payment_postfinance_transaction_id", None)

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
            external_id = f"pretix-refund-{refund.order.code}-R-{refund.local_id}"
            merchant_reference = f"pretix-{self.event.slug}-{refund.order.code}-R-{refund.local_id}"

            postfinance_refund = client.refund_transaction(
                transaction_id=int(transaction_id),
                external_id=external_id,
                merchant_reference=merchant_reference,
                amount=refund.amount,
            )

            # Store refund info on the OrderRefund object
            refund.info = json.dumps(
                {
                    "refund_id": postfinance_refund.id,
                    "state": postfinance_refund.state.value if postfinance_refund.state else None,
                    "amount": float(postfinance_refund.amount)
                    if postfinance_refund.amount
                    else None,
                    "created_on": str(postfinance_refund.created_on)
                    if postfinance_refund.created_on
                    else None,
                }
            )
            refund.state = OrderRefund.REFUND_STATE_TRANSIT
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
            refund.info = json.dumps(refund_info_data)
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
