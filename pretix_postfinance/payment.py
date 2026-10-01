from __future__ import annotations

import logging
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse, urlunparse

from django import forms
from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from i18nfield.forms import I18nFormField, I18nTextarea, I18nTextInput
from i18nfield.strings import LazyI18nString
from postfinancecheckout.models import (
    AddressCreate,
    LineItemCreate,
    LineItemType,
    TransactionState,
)
from pretix.base.forms import SecretKeySettingsField
from pretix.base.models import OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.base.templatetags.money import money_filter
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri

from .api import PostFinanceClient, PostFinanceError

if TYPE_CHECKING:
    from pretix.base.models import Event, Order

logger = logging.getLogger(__name__)


# PostFinance transaction states that indicate payment is settled.
# Only FULFILL guarantees funds have been received — for asynchronous
# payment methods like bank transfers, COMPLETED means the transfer was
# initiated but money has not yet arrived.
SUCCESS_STATES = {
    TransactionState.FULFILL,
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

# info_data keys describing a payment charged in a different currency than
# the event's. Written when a transaction is created, they snapshot the
# conversion so refunds and displays use the rate the customer was charged
# at, not the current setting.
FX_INFO_KEYS = (
    "fx_rate",
    "fx_base_currency",
    "charged_currency",
    "charged_amount",
)

# info_data key holding the ID of the space a transaction was created in.
# Transaction and refund IDs are only unique within a space, so the space is
# part of an entity's identity and must be persisted alongside the ID.
SPACE_ID_KEY = "space_id"

MAIN_PROVIDER_IDENTIFIER = "postfinance"
PROD_PROVIDER_IDENTIFIER = "postfinance_prod"

# Setting that opts an event into offering the production space as a second
# payment option while in test mode. Off unless an organizer turns it on.
PROD_SPACE_IN_TESTMODE_KEY = "prod_space_in_testmode"

# All provider identifiers used by this plugin. Payments and refunds may be
# stored under any of these.
PROVIDER_IDENTIFIERS = (MAIN_PROVIDER_IDENTIFIER, PROD_PROVIDER_IDENTIFIER)

WEBHOOK_URL_NAME = "plugins:pretix_postfinance:postfinance.webhook"

# pretix.cfg section and option overriding the host PostFinance calls back on.
WEBHOOK_BASE_URL_SECTION = "postfinance"
WEBHOOK_BASE_URL_OPTION = "webhook_base_url"


def _configured_webhook_base_url() -> str:
    """Read the webhook base URL override from pretix.cfg, if it is set."""
    config = getattr(django_settings, "CONFIG_FILE", None)
    if config is None:
        return ""
    return (
        config.get(
            WEBHOOK_BASE_URL_SECTION,
            WEBHOOK_BASE_URL_OPTION,
            fallback="",
        )
        or ""
    ).strip()


def webhook_url() -> str:
    """
    The URL PostFinance should send callbacks to.

    Normally this instance's own address. An instance PostFinance cannot reach
    under that address — staging behind a VPN, say — can name a different host
    in pretix.cfg:

        [postfinance]
        webhook_base_url = https://pretix-staging.example.org

    or through the matching PRETIX_POSTFINANCE_WEBHOOK_BASE_URL environment
    variable. Only the scheme and host are taken from it; the path always comes
    from the URL config, so an override cannot drift away from the real route.
    """
    built = urlparse(build_global_uri(WEBHOOK_URL_NAME))

    base = _configured_webhook_base_url()
    if not base:
        return urlunparse(built)

    # Without a scheme urlparse reads "host" as a path and "host:8443" as a
    # scheme, so neither yields a netloc; assume https and re-parse.
    override = urlparse(base if "://" in base else f"https://{base}")
    if not override.netloc:
        logger.warning(
            "Ignoring [%s] %s=%r: no host in it",
            WEBHOOK_BASE_URL_SECTION,
            WEBHOOK_BASE_URL_OPTION,
            base,
        )
        return urlunparse(built)

    return urlunparse(built._replace(scheme=override.scheme, netloc=override.netloc))


class PostFinancePaymentProvider(BasePaymentProvider):
    """
    PostFinance Checkout payment provider for pretix.

    Enables Swiss payment methods including Card, E-Finance, and TWINT
    through the PostFinance Checkout API.
    """

    identifier = MAIN_PROVIDER_IDENTIFIER
    verbose_name = "PostFinance"
    abort_pending_allowed = False
    execute_payment_needs_user = True
    payment_form_template_name = "pretixplugins/postfinance/checkout_payment_form.html"

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

    def _mode_for_payment(self, payment: OrderPayment) -> Literal["live", "test"]:
        """
        Return which space the payment's transaction lives in.

        Prefers the space that was recorded when the transaction was created.
        Payments created before the space was persisted, or whose recorded
        space is no longer configured, fall back to the order's test mode
        flag: those were created in the test space if test credentials were
        configured at the time.
        """
        recorded_space_id = (payment.info_data or {}).get(SPACE_ID_KEY)
        if recorded_space_id:
            test_space_id = self.settings.get("test_space_id")
            if test_space_id and str(recorded_space_id) == str(test_space_id):
                return "test"
            space_id = self.settings.get("space_id")
            if space_id and str(recorded_space_id) == str(space_id):
                return "live"

        if payment.order.testmode and self._has_test_credentials():
            return "test"
        return "live"

    def _space_id_for_payment(self, payment: OrderPayment) -> str | None:
        """
        Return the ID of the space the payment's transaction lives in.
        """
        recorded_space_id = (payment.info_data or {}).get(SPACE_ID_KEY)
        if recorded_space_id:
            return str(recorded_space_id)
        return self._get_credentials_for_mode(self._mode_for_payment(payment))[0]

    @property
    def _base_public_name(self) -> str:
        """
        Return the configured display name without any space suffix.

        If a custom display name is configured in event settings, use that.
        Otherwise fall back to the default name 'PostFinance'.
        """
        name = self.settings.get("public_name", as_type=LazyI18nString)
        if name:
            return str(name)
        return PostFinancePaymentProvider.verbose_name

    def _prod_space_offered_in_testmode(self) -> bool:
        """
        Check whether the production space is offered as a second option.

        This has to be switched on explicitly in the payment settings, which
        organizers may restrict to administrators. Without test credentials
        the main provider already uses the production space in test mode, so
        the second option would be redundant.
        """
        return bool(
            self.event.testmode
            and self._has_test_credentials()
            and self.settings.get(PROD_SPACE_IN_TESTMODE_KEY, as_type=bool, default=False)
        )

    @property
    def public_name(self) -> str:
        """
        Return the name shown to customers during checkout.

        While the production space is offered alongside this provider, the
        name is suffixed with "(test space)" to tell the two apart. On its
        own there is nothing to distinguish it from, so the name is left
        alone.
        """
        if self._prod_space_offered_in_testmode():
            return f"{self._base_public_name} ({_('test space')})"
        return self._base_public_name

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
                    I18nFormField(
                        label=_("Display name"),
                        help_text=_(
                            "Custom name shown to customers during checkout. "
                            "Leave empty to use the default name 'PostFinance'."
                        ),
                        widget=I18nTextInput,
                        required=False,
                    ),
                ),
                (
                    "description",
                    I18nFormField(
                        label=_("Description"),
                        help_text=_(
                            "Custom description shown on the checkout page. "
                            "Leave empty to use the default message."
                        ),
                        widget=I18nTextarea,
                        widget_kwargs={"attrs": {"rows": 3}},
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
                    PROD_SPACE_IN_TESTMODE_KEY,
                    forms.BooleanField(
                        label=_("Offer the production space in test mode"),
                        help_text=_(
                            "While the event is in test mode, offer a second payment "
                            "option that charges through your production space, so it "
                            "can be verified end-to-end before going live. Payments "
                            "made through it are real charges and are not undone by "
                            "deleting the test mode orders. Requires test credentials; "
                            "without them test mode already uses the production space."
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
                (
                    "alt_currency",
                    forms.ChoiceField(
                        label=_("Alternative payment currency"),
                        help_text=_(
                            "Offer customers the choice to be charged in this currency "
                            "instead of {currency}. pretix keeps all accounting (orders, "
                            "invoices, refunds) in {currency}; only the charge sent to "
                            "PostFinance is converted. Requires an exchange rate below."
                        ).format(currency=self.event.currency),
                        choices=[("", _("None"))]
                        + [
                            (c.alpha_3, f"{c.alpha_3} - {c.name}")
                            for c in django_settings.CURRENCIES
                            if c.alpha_3 != self.event.currency
                        ],
                        required=False,
                    ),
                ),
                (
                    "alt_currency_rate",
                    forms.DecimalField(
                        label=_("Exchange rate"),
                        help_text=_(
                            "How much customers are charged in the alternative currency "
                            "per 1 {currency}. The rate in effect when a payment is "
                            "started is kept for its refunds. Include a small margin to "
                            "cover exchange rate fluctuations."
                        ).format(currency=self.event.currency),
                        min_value=Decimal("0.000001"),
                        max_digits=12,
                        decimal_places=6,
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

        if cleaned_data.get("alt_currency") and not cleaned_data.get("alt_currency_rate"):
            raise ValidationError(
                _("An exchange rate is required when an alternative payment currency is set.")
            )

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
            "webhook_url": webhook_url(),
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

    def _get_client_for_payment(self, payment: OrderPayment) -> PostFinanceClient:
        """
        Create a client for the space the payment's transaction lives in.

        Actions on an existing transaction (refunds, status lookups) must talk
        to the space that transaction was created in, which is not necessarily
        the space `event.testmode` points at today.
        """
        return self._get_client_for_mode(self._mode_for_payment(payment))

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

    @property
    def alt_currency_config(self) -> tuple[str, Decimal] | None:
        """
        The configured alternative payment currency and exchange rate.

        Returns ``None`` unless a valid alternative currency different from
        the event currency and a positive rate are configured.
        """
        currency = self.settings.get("alt_currency")
        if not currency or currency == self.event.currency:
            return None
        try:
            rate = self.settings.get("alt_currency_rate", as_type=Decimal)
        except (ArithmeticError, TypeError, ValueError):
            # A rate that cannot be parsed as a decimal disables the feature
            # rather than breaking checkout, but it is a misconfiguration the
            # organizer needs to hear about.
            logger.warning(
                "Ignoring unparseable PostFinance exchange rate %r for event %s",
                self.settings.get("alt_currency_rate"),
                self.event.slug,
            )
            return None
        if not rate or rate <= 0:
            return None
        return str(currency), rate

    def _convert(self, amount: Decimal, rate: Decimal) -> Decimal:
        return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _format_rate(rate: Decimal) -> str:
        """
        Format an exchange rate for display without scientific notation.

        ``Decimal.normalize()`` strips trailing zeros, but turns whole rates
        like 160 into ``1.6E+2``, so the plain fixed-point form is used.
        """
        return format(rate.normalize(), "f")

    @property
    def _session_pay_alt_key(self) -> str:
        return f"payment_{self.identifier}_pay_alt"

    @property
    def payment_form_fields(self) -> OrderedDict:
        fields: OrderedDict = OrderedDict()
        config = self.alt_currency_config
        if config:
            currency, _rate = config
            fields["pay_alt"] = forms.BooleanField(
                label=_("Pay in {currency}").format(currency=currency),
                required=False,
            )
        return fields

    def payment_form_render(
        self, request: HttpRequest, total: Decimal, order: Order | None = None
    ) -> str:
        form = self.payment_form(request)
        config = self.alt_currency_config
        if config and "pay_alt" in form.fields:
            currency, rate = config
            form.fields["pay_alt"].help_text = _(
                "You will be charged {alt_amount} instead of {base_amount} "
                "(exchange rate: 1 {base_currency} = {rate} {currency})."
            ).format(
                alt_amount=money_filter(self._convert(total, rate), currency),
                base_amount=money_filter(total, self.event.currency),
                base_currency=self.event.currency,
                rate=self._format_rate(rate),
                currency=currency,
            )
        template = get_template(self.payment_form_template_name)
        return template.render({"request": request, "form": form})

    def _fx_from_request(
        self, request: HttpRequest, payment: OrderPayment
    ) -> dict[str, str] | None:
        """
        Resolve the customer's currency choice into an FX snapshot.

        Returns ``None`` when the payment should be charged in the event
        currency, or a dict of ``FX_INFO_KEYS`` when the customer opted to
        pay in the alternative currency.
        """
        config = self.alt_currency_config
        if config is None or not request.session.get(self._session_pay_alt_key):
            return None
        currency, rate = config
        return {
            "fx_rate": str(rate),
            "fx_base_currency": str(self.event.currency),
            "charged_currency": currency,
            "charged_amount": str(self._convert(payment.amount, rate)),
        }

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

    @property
    def _session_transaction_id_key(self) -> str:
        return f"payment_{self.identifier}_transaction_id"

    @property
    def _session_transaction_payment_id_key(self) -> str:
        return f"payment_{self.identifier}_transaction_payment_id"

    def _get_prepared_transaction_id(
        self,
        request: HttpRequest,
        payment: OrderPayment | None = None,
    ) -> int | None:
        if payment is not None:
            if payment_transaction_id := self._get_payment_transaction_id(payment):
                return payment_transaction_id

            session_transaction_id = request.session.get(self._session_transaction_id_key)
            session_payment_id = request.session.get(self._session_transaction_payment_id_key)
            if session_transaction_id is not None and session_payment_id == payment.pk:
                return int(session_transaction_id)

            return None

        session_transaction_id = request.session.get(self._session_transaction_id_key)
        if session_transaction_id is not None:
            return int(session_transaction_id)

        return self._get_payment_transaction_id(payment or self._get_request_payment(request))

    def _set_session_transaction_id(
        self, request: HttpRequest, payment: OrderPayment, transaction_id: int
    ) -> None:
        request.session[self._session_transaction_id_key] = transaction_id
        request.session[self._session_transaction_payment_id_key] = payment.pk

    def _clear_session_transaction_id(self, request: HttpRequest) -> None:
        request.session.pop(self._session_transaction_id_key, None)
        request.session.pop(self._session_transaction_payment_id_key, None)

    def _set_pending_transaction_id(
        self,
        payment: OrderPayment,
        transaction_id: int,
        space_id: int,
        fx: dict[str, str] | None = None,
    ) -> None:
        info = payment.info_data
        # Drop any FX snapshot from a previous attempt: a retry may have
        # switched between event currency and alternative currency.
        for key in FX_INFO_KEYS:
            info.pop(key, None)
        info[PENDING_TRANSACTION_ID_KEY] = transaction_id
        info[SPACE_ID_KEY] = space_id
        if fx:
            info.update(fx)
        payment.info_data = info
        payment.save(update_fields=["info"])

    def _clear_pending_transaction_id(self, payment: OrderPayment) -> None:
        info = payment.info_data
        if info.pop(PENDING_TRANSACTION_ID_KEY, None) is None:
            return

        payment.info_data = info
        payment.save(update_fields=["info"])

    def _build_payment_transaction_line_items(
        self,
        payment: OrderPayment,
        detailed_line_items: bool = False,
        fx: dict[str, str] | None = None,
    ) -> list[LineItemCreate]:
        if fx:
            # A single aggregate line item is used for converted charges,
            # even when detailed line items were requested: PostFinance
            # requires line items to sum to the transaction amount, and
            # converting each position separately would accumulate
            # rounding drift.
            return [
                LineItemCreate(
                    name=str(_("Payment for order {code}")).format(code=payment.order.code),
                    quantity=1,
                    amountIncludingTax=float(Decimal(fx["charged_amount"])),
                    type=LineItemType.PRODUCT,
                    uniqueId=f"payment-{payment.pk}",
                )
            ]

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
        self,
        payment: OrderPayment,
        detailed_line_items: bool = False,
        fx: dict[str, str] | None = None,
    ) -> tuple[int, str, int]:
        """
        Create a PostFinance transaction for a payment.

        Returns:
            A tuple of (transaction ID, payment page URL, space ID). The space
            is part of the transaction's identity and must be stored with it.
        """
        client = self._get_client()
        line_items = self._build_payment_transaction_line_items(
            payment, detailed_line_items=detailed_line_items, fx=fx
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
            currency=fx["charged_currency"] if fx else payment.order.event.currency,
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

        return transaction_id, payment_page_url, client.space_id

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
            fx = self._fx_from_request(request, payment)
            transaction_id, payment_page_url, space_id = self._create_payment_transaction(
                payment, fx=fx
            )
            self._set_session_transaction_id(request, payment, transaction_id)
            self._set_pending_transaction_id(payment, transaction_id, space_id, fx=fx)
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
            "description": self.settings.get("description", as_type=LazyI18nString),
        }
        html = template.render(ctx)

        config = self.alt_currency_config
        if config and request.session.get(self._session_pay_alt_key):
            currency, rate = config
            html += format_html(
                "<p>{}</p>",
                _(
                    "You will be charged in {currency} "
                    "(exchange rate: 1 {base_currency} = {rate} {currency})."
                ).format(
                    currency=currency,
                    base_currency=self.event.currency,
                    rate=self._format_rate(rate),
                ),
            )
        return html

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str | None:
        """
        Execute the payment after the order is confirmed.

        Retrieves the transaction details from PostFinance, checks the
        transaction state, and confirms or fails the payment accordingly.
        """
        transaction_id = self._get_prepared_transaction_id(request, payment)

        if not transaction_id:
            try:
                fx = self._fx_from_request(request, payment)
                transaction_id, payment_page_url, space_id = self._create_payment_transaction(
                    payment,
                    detailed_line_items=request.method == "GET",
                    fx=fx,
                )
                self._set_pending_transaction_id(payment, transaction_id, space_id, fx=fx)
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
            client = self._get_client_for_payment(payment)
            transaction = client.get_transaction(transaction_id)

            payment_method = None
            if transaction.payment_connector_configuration:
                payment_method = transaction.payment_connector_configuration.name

            state = transaction.state
            info_data = payment.info_data or {}
            info_data.pop(PENDING_TRANSACTION_ID_KEY, None)
            info_data.update(
                {
                    "transaction_id": transaction_id,
                    SPACE_ID_KEY: client.space_id,
                    "state": state.value if state else None,
                    "payment_method": payment_method,
                    "created_on": str(transaction.created_on)
                    if transaction.created_on
                    else None,
                }
            )
            payment.info_data = info_data
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
                payment.refresh_from_db()
                if payment.state == OrderPayment.PAYMENT_STATE_CREATED:
                    cast(Any, payment).state = OrderPayment.PAYMENT_STATE_PENDING
                    payment.save(update_fields=["state"])
                logger.info(
                    "Payment %s is pending (PostFinance state: %s)",
                    payment.pk,
                    state,
                )

        except PostFinanceError as e:
            logger.exception("PostFinance API error during execute_payment: %s", e)
            payment.info_data = {
                "transaction_id": transaction_id,
                SPACE_ID_KEY: self._space_id_for_payment(payment),
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
                SPACE_ID_KEY: self._space_id_for_payment(payment),
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
        space_id = self._space_id_for_payment(payment)
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
        base = (
            f"{self.public_name} ({payment_method})"
            if payment_method
            else str(self.public_name)
        )

        charged_amount = info_data.get("charged_amount")
        charged_currency = info_data.get("charged_currency")
        if charged_amount and charged_currency:
            return str(_("{payment_method} — charged as {alt_amount}")).format(
                payment_method=base,
                alt_amount=money_filter(Decimal(str(charged_amount)), charged_currency),
            )
        return base

    def payment_control_render_short(self, payment: OrderPayment) -> str:
        """
        Return a very short version of the payment method for admin actions.
        """
        info_data = payment.info_data or {}
        transaction_id = info_data.get("transaction_id")
        if transaction_id:
            return f"{self.verbose_name} ({transaction_id})"
        return str(self.verbose_name)

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

    @staticmethod
    def _unknown_rate_exception(charged_currency: str) -> PaymentException:
        return PaymentException(
            str(
                _(
                    "The exchange rate used for this payment is unknown, so the refund "
                    "amount cannot be converted to {currency}. Please refund manually "
                    "in the PostFinance dashboard."
                ).format(currency=charged_currency)
            )
        )

    def _charged_amount_of(
        self, refund: OrderRefund, rate: Decimal | None, charged_currency: str
    ) -> Decimal:
        """
        Return how much of the transaction's charge a refund draws down.

        Refunds this plugin sent store the amount PostFinance booked, which
        is authoritative. Refunds recorded without one — externally created
        in the dashboard, or not yet sent — are converted at the payment's
        rate, the same way sending them would have.
        """
        booked = (refund.info_data or {}).get("amount")
        if booked is not None:
            return Decimal(str(booked))
        if rate is None:
            raise self._unknown_rate_exception(charged_currency)
        return self._convert(cast("Decimal", refund.amount), rate)

    def _refund_transaction_amount(self, refund: OrderRefund) -> Decimal:
        """
        Return the refund amount in the transaction's charge currency.

        Payments charged in the event currency refund their amount as-is.
        For payments charged in the alternative currency, partial refunds
        are converted with the rate snapshotted on the payment; a refund
        that returns the payment's full remaining amount instead draws down
        what is left of the charge recorded at payment time, so conversion
        rounding can neither leave a cent behind nor overshoot the
        transaction. PostFinance rejects a refund carrying neither an amount
        nor line item reductions, so an amount is always sent.
        """
        payment = refund.payment
        info = payment.info_data or {}
        charged_currency = str(info.get("charged_currency") or "")
        if not charged_currency or charged_currency == self.event.currency:
            return cast("Decimal", refund.amount)

        rate_raw = info.get("fx_rate")
        rate = Decimal(str(rate_raw)) if rate_raw else None

        # Only refunds drawn on the PostFinance transaction reduce what is
        # left to refund there. A refund settled by other means (manual bank
        # transfer, gift card, offsetting) leaves the transaction untouched,
        # so counting it would make the next refund look like the remainder
        # and refund the whole outstanding charge.
        siblings = list(
            payment.refunds.exclude(pk=refund.pk).filter(
                provider__in=PROVIDER_IDENTIFIERS,
                state__in=(
                    OrderRefund.REFUND_STATE_DONE,
                    OrderRefund.REFUND_STATE_TRANSIT,
                    OrderRefund.REFUND_STATE_CREATED,
                    OrderRefund.REFUND_STATE_EXTERNAL,
                ),
            )
        )
        already_refunded = sum(
            (cast("Decimal", sibling.amount) for sibling in siblings), Decimal("0.00")
        )

        charged_amount_raw = info.get("charged_amount")
        if charged_amount_raw and refund.amount >= payment.amount - already_refunded:
            remaining = Decimal(str(charged_amount_raw)) - sum(
                (
                    self._charged_amount_of(sibling, rate, charged_currency)
                    for sibling in siblings
                ),
                Decimal("0.00"),
            )
            if remaining <= 0:
                raise PaymentException(
                    str(
                        _(
                            "This payment has already been fully refunded in {currency}. "
                            "Please check the PostFinance dashboard."
                        ).format(currency=charged_currency)
                    )
                )
            return remaining

        if rate is None:
            raise self._unknown_rate_exception(charged_currency)
        return self._convert(cast("Decimal", refund.amount), rate)

    def execute_refund(self, refund: OrderRefund, user: str = "system") -> None:
        """
        Execute a refund for an order.

        This is called by pretix when a refund needs to be processed.

        Args:
            refund: The OrderRefund to process.
            user: The user performing the action (for audit logging).
        """
        payment = cast(OrderPayment, refund.payment)
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
            client = self._get_client_for_payment(payment)

            # Generate a unique external ID for idempotency
            external_id = f"pretix-{self.event.slug}-{refund.order.code}-R-{refund.local_id}"
            merchant_reference = f"{self.event.slug}-{refund.order.code}"

            postfinance_refund = client.refund_transaction(
                transaction_id=int(transaction_id),
                external_id=external_id,
                merchant_reference=merchant_reference,
                amount=self._refund_transaction_amount(refund),
            )

            # Store refund info on the OrderRefund object
            refund.info_data = {
                "refund_id": postfinance_refund.id,
                SPACE_ID_KEY: client.space_id,
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
            return f"{self.verbose_name} ({refund_id})"
        return str(self.verbose_name)

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
                SPACE_ID_KEY: info_data.get(SPACE_ID_KEY),
                "state": info_data.get("state"),
                "_shredded": True,
            }
            obj.save(update_fields=["info"])
        else:
            # For refunds, clear the info
            obj.info_data = {"_shredded": True}
            obj.save(update_fields=["info"])


class PostFinanceProdSpacePaymentProvider(PostFinancePaymentProvider):
    """
    PostFinance provider that always uses the production space.

    Offered during checkout only while the event is in test mode, test
    credentials are configured and the option is switched on in the payment
    settings, so organizers can verify their production space end-to-end
    before taking the event live. Payments made through it are real charges
    in the production space.

    It shares all settings (credentials, display name, ...) with the main
    provider and therefore has no settings section of its own.
    """

    identifier = PROD_PROVIDER_IDENTIFIER
    verbose_name = _("PostFinance (production space)")

    def __init__(self, event: Event) -> None:
        super().__init__(event)
        # Share the settings namespace with the main provider so both use the
        # same credentials and _enabled flag.
        self.settings = SettingsSandbox(
            "payment", PostFinancePaymentProvider.identifier, self.event
        )

    @property
    def settings_form_fields(self) -> OrderedDict:
        # No own settings section — everything is configured on the main
        # provider, whose settings this provider shares.
        return OrderedDict()

    def settings_content_render(self, request: HttpRequest) -> str:
        return ""

    @property
    def public_name(self) -> str:
        """
        Return the name shown to customers during checkout.

        Always suffixed with "(production space)" to distinguish it from the
        test space provider offered alongside it in test mode.
        """
        return f"{self._base_public_name} ({_('production space')})"

    @property
    def test_mode_message(self) -> str:
        """
        Return a warning that this provider makes real charges in test mode.
        """
        return str(
            _(
                "This payment method uses your production PostFinance space. "
                "Payments are real and will be charged even though the event "
                "is in test mode."
            )
        )

    def is_allowed(self, request: HttpRequest, total: Decimal | None = None) -> bool:
        """
        Only offer this provider during checkout while it is switched on.
        """
        if not self._prod_space_offered_in_testmode():
            return False
        # pretix's own signatures spell these parameters as non-optional even
        # though they accept None, hence the casts.
        return super().is_allowed(request, cast(Any, total))

    def order_change_allowed(self, order: Order, request: HttpRequest | None = None) -> bool:
        """
        Only offer this provider for payment retries while it is switched on.

        `is_allowed()` only covers checkout; retrying an unpaid order or
        changing its payment method goes through this hook instead, so it
        needs the same gate or the provider shows up on live events.
        """
        if not self._prod_space_offered_in_testmode():
            return False
        return super().order_change_allowed(order, cast(Any, request))

    def _get_credentials(self) -> tuple[str | None, str | None, str | None]:
        """Always use the production space credentials."""
        return self._get_credentials_for_mode("live")

    def _get_client(self) -> PostFinanceClient:
        """Always create a client for the production space."""
        return self._get_client_for_mode("live")

    def _mode_for_payment(self, payment: OrderPayment) -> Literal["live", "test"]:
        """Transactions of this provider always live in the production space."""
        return "live"
