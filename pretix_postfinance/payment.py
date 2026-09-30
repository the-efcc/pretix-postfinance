from __future__ import annotations

import logging
from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

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
    ChargeState,
    CustomersPresence,
    LineItemCreate,
    LineItemType,
    TokenizationMode,
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
from .installments import (
    installments_available,
    plan_for_order,
    scheduled_installment_for_payment,
)

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

# Key under which the ID of the reusable PostFinance token a payment is
# associated with is recorded: the token an interactive installment payment
# created, or the one an automatic charge was made against.
TOKEN_ID_KEY = "token_id"

# Marks a payment as an automatic charge against a plan's stored token,
# rather than one the customer made on the payment page.
INSTALLMENT_CHARGE_KEY = "installment_charge"

# info_data key naming the scheduled installment an automatic charge was made
# for. pretix builds a fresh OrderPayment for every attempt, so this is what
# ties the attempts at one installment together and lets a retry find out what
# became of the transaction the attempt before it left behind.
INSTALLMENT_ID_KEY = "installment_id"

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

# Setting recording which space the saved `allowed_payment_methods` IDs were
# read from. Payment method configuration IDs are scoped to a space, so a list
# collected in one space means nothing in the other.
ALLOWED_METHODS_SPACE_KEY = "allowed_payment_methods_space"

# How long the payment method list fetched from PostFinance stays good for.
# Short enough that a newly configured method shows up without anyone having
# to know about the cache, long enough that repeated settings page renders
# do not each wait on the API.
METHOD_CHOICES_CACHE_TIMEOUT = 300

# All provider identifiers used by this plugin. Payments and refunds may be
# stored under any of these.
PROVIDER_IDENTIFIERS = (MAIN_PROVIDER_IDENTIFIER, PROD_PROVIDER_IDENTIFIER)


def _line_item_type(amount: Decimal, positive_type: LineItemType) -> LineItemType:
    """
    Pick the line item type PostFinance will accept for this amount.

    PostFinance requires a negative line item to be a DISCOUNT and rejects the
    transaction outright otherwise. pretix puts no lower bound on an
    `OrderFee`, so an organizer entering a negative fee in the order change
    form — the usual way to record an ad-hoc discount — is enough to produce
    one.
    """
    if amount < 0:
        return LineItemType.DISCOUNT
    return positive_type


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
    def installments_supported(self) -> bool:
        """
        Whether this provider can carry an installment plan.

        Tokenised follow-up charges are implemented here, so the only
        thing that can be missing is pretix's own installment support:
        upstream pretix has no plans to attach a token to.
        """
        return installments_available()

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
            return _("{name} (test space)").format(name=self._base_public_name)
        return self._base_public_name

    def _get_payment_method_choices(self) -> list[tuple[str, str]]:
        """
        Fetch available payment method configurations from PostFinance.

        Always read from the production space, whatever mode the event is in.
        Configuration IDs are scoped to a space, and the saved list is what
        real charges are restricted by, so collecting it anywhere else would
        put IDs into the setting that the production space does not know —
        and every payment would fail the moment the event goes live.

        Returns a list of (id, name) tuples for use in a MultipleChoiceField.
        Returns an empty list if credentials are not configured or API call fails.
        """
        space_id, user_id, auth_key = self._get_credentials_for_mode("live")

        if not all([space_id, user_id, auth_key]):
            return []

        # Every render of the payment settings — including the list of all
        # payment providers, which evaluates `settings_form_fields` for each
        # one — would otherwise wait on a live API call, up to the client's
        # full timeout.
        cache_key = f"postfinance_payment_methods_{space_id}"
        cached = self.event.cache.get(cache_key)
        if cached is not None:
            return [(str(value), str(label)) for value, label in cached]

        try:
            client = self._get_client_for_mode("live")
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
            choices = sorted(choices, key=lambda x: x[1])
        except Exception:
            # Settings form rendering must never break on bad credentials.
            # The SDK raises non-PostFinanceError exceptions for malformed
            # auth keys (e.g. binascii.Error) before any HTTP call.
            # Deliberately not cached: the next render should try again.
            logger.warning("Failed to fetch payment method configurations", exc_info=True)
            return []

        # Outside the guard above, so a cache backend problem cannot be
        # mistaken for the space having no payment methods.
        self.event.cache.set(cache_key, choices, timeout=METHOD_CHOICES_CACHE_TIMEOUT)
        return choices

    def _method_choices_with_saved(
        self, choices: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """
        Add any saved-but-unlisted method IDs to the choice list.

        A `MultipleChoiceField` rejects a value that is not among its choices,
        so when the API is unreachable — or a configuration is retired at
        PostFinance — the saved IDs would fail validation and the organizer
        could not save the settings page at all, including the fields that
        have nothing to do with payment methods. Carrying the saved values
        through keeps the form saveable and shows what is currently selected.
        """
        known = {value for value, _label in choices}
        saved = self.settings.get("allowed_payment_methods", as_type=list) or []
        extra = sorted(
            {str(value) for value in saved if value and str(value) not in known}
        )
        if not extra:
            return choices
        return choices + [
            (
                value,
                str(_("Configuration {id} (name unavailable)")).format(id=value),
            )
            for value in extra
        ]

    def _parse_allowed_payment_methods(self, space_id: int | None = None) -> list[int] | None:
        """
        Parse the allowed_payment_methods setting for the space being charged.

        The saved IDs belong to the space they were read from, which is
        recorded alongside them. Sending them to a different space would name
        configurations it has never heard of and the transaction would be
        rejected, so a list that does not belong to `space_id` is dropped and
        the charge goes ahead unrestricted rather than failing outright.

        Returns:
            List of payment method configuration IDs, or None if all methods allowed.
        """
        allowed_methods = self.settings.get("allowed_payment_methods", as_type=list)

        if not allowed_methods:
            return None

        recorded_space = self.settings.get(ALLOWED_METHODS_SPACE_KEY)
        if space_id is not None and recorded_space and str(recorded_space) != str(space_id):
            logger.warning(
                "Ignoring allowed_payment_methods for event %s: the list belongs to "
                "space %s but the transaction is being created in space %s",
                self.event.slug,
                recorded_space,
                space_id,
            )
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
                    ALLOWED_METHODS_SPACE_KEY,
                    forms.CharField(
                        required=False,
                        widget=forms.HiddenInput,
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
                        choices=self._method_choices_with_saved(payment_method_choices),
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

        # Saving may well be how an organizer fixes wrong credentials or adds
        # a method at PostFinance, so the cached list must not outlive it.
        # `event.cache` is documented to clear itself when related objects
        # change; not depending on that covering hierarkey's settings rows.
        if cleaned_data.get("space_id"):
            self.event.cache.delete(
                f"postfinance_payment_methods_{cleaned_data['space_id']}"
            )

        # The choice list is read from the production space, so that is the
        # space the saved IDs belong to. Set it here rather than trusting what
        # the hidden field came back with.
        if "allowed_payment_methods" in cleaned_data:
            cleaned_data[ALLOWED_METHODS_SPACE_KEY] = (
                str(cleaned_data.get("space_id") or "")
                if cleaned_data["allowed_payment_methods"]
                else ""
            )

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

    def _get_order_merchant_reference(
        self, order_code: str, installment_number: int | None = None
    ) -> str:
        merchant_reference = f"{self.event.slug}-{order_code}"
        if installment_number is not None:
            merchant_reference = f"{merchant_reference}-inst-{installment_number}"
        return merchant_reference

    def _payment_line_item_name(
        self, payment: OrderPayment, installment: Any | None = None
    ) -> str:
        """
        Describe on the payment page what the customer is paying for.

        An installment says which one it is, so the PostFinance receipt of a
        plan's first charge is not indistinguishable from a payment of the
        whole order.
        """
        if installment is not None:
            return str(_("Installment {number} of {count} for order {code}")).format(
                number=installment.installment_number,
                count=installment.plan.total_installments,
                code=payment.order.code,
            )
        return str(_("Payment for order {code}")).format(code=payment.order.code)

    def _build_payment_transaction_line_items(
        self,
        payment: OrderPayment,
        detailed_line_items: bool = False,
        fx: dict[str, str] | None = None,
        installment: Any | None = None,
    ) -> list[LineItemCreate]:
        if fx:
            # A single aggregate line item is used for converted charges,
            # even when detailed line items were requested: PostFinance
            # requires line items to sum to the transaction amount, and
            # converting each position separately would accumulate
            # rounding drift.
            return [
                LineItemCreate(
                    name=self._payment_line_item_name(payment, installment),
                    quantity=1,
                    amountIncludingTax=float(Decimal(fx["charged_amount"])),
                    type=LineItemType.PRODUCT,
                    uniqueId=f"payment-{payment.pk}",
                )
            ]

        if detailed_line_items and payment.amount == payment.order.total:
            line_items = self._build_line_items(
                {
                    "positions": payment.order.positions.filter(canceled=False).select_related(
                        "item", "variation"
                    ),
                    "fees": payment.order.fees.filter(canceled=False),
                    "total": payment.amount,
                },
                payment.order.event.currency,
            )
            # PostFinance rejects a transaction whose line items do not sum to
            # its amount. `order.total` is that sum by construction, but an
            # order it does not hold for must still be payable, so fall back
            # to the single aggregate line rather than sending something that
            # cannot be accepted.
            amounts = [getattr(item, "amount_including_tax", None) for item in line_items]
            if any(amount is None for amount in amounts):
                # Nothing to check against; send what was built, as before.
                return line_items
            itemised_total = sum(
                (Decimal(str(amount)) for amount in amounts), Decimal("0.00")
            )
            if itemised_total == payment.amount:
                return line_items
            logger.warning(
                "Itemised line items for payment %s sum to %s, not %s; "
                "falling back to a single line item",
                payment.pk,
                itemised_total,
                payment.amount,
            )

        return [
            LineItemCreate(
                name=self._payment_line_item_name(payment, installment),
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
        installment = scheduled_installment_for_payment(payment)
        line_items = self._build_payment_transaction_line_items(
            payment,
            detailed_line_items=detailed_line_items,
            fx=fx,
            installment=installment,
        )
        installment_number = None
        tokenization_mode = None
        if installment is not None:
            # The customer is present for this one, so it goes through the
            # normal payment page; forcing tokenization is what leaves behind
            # a token the later installments can be charged against.
            installment_number = installment.installment_number
            tokenization_mode = TokenizationMode.FORCE_CREATION
            logger.info(
                "Enabling tokenization for installment %s of payment %s",
                installment_number,
                payment.pk,
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
            merchant_reference=self._get_order_merchant_reference(
                payment.order.code,
                installment_number=installment_number,
            ),
            allowed_payment_method_configurations=self._parse_allowed_payment_methods(
                client.space_id
            ),
            tokenization_mode=tokenization_mode,
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
                    type=_line_item_type(price, LineItemType.PRODUCT),
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
                    type=_line_item_type(fee_value, LineItemType.FEE),
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
                payment, detailed_line_items=True, fx=fx
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
                    detailed_line_items=True,
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
                # Error details are added to what the payment already knows,
                # never substituted for it. Replacing info_data wholesale
                # drops the FX snapshot, and a payment charged in the
                # alternative currency that loses it looks like a plain
                # event-currency payment to `_refund_transaction_amount()` —
                # which would then refund the event-currency amount out of a
                # transaction booked in another currency.
                payment.info_data = {
                    **(payment.info_data or {}),
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
                payment.info_data = {**(payment.info_data or {}), "error": str(e)}
                payment.save(update_fields=["info"])
                raise
            except Exception as e:
                logger.exception(
                    "Unexpected error during execute_payment transaction creation: %s",
                    e,
                )
                payment.info_data = {
                    **(payment.info_data or {}),
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

                # A plan's first payment leaves behind the token every
                # later installment is charged against, so it has to be
                # stored before the payment is confirmed.
                self.store_installment_token(payment, transaction)

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
                # `fail()` assigns whatever it is given to info_data rather
                # than merging it, so the transaction reference, the space and
                # the FX snapshot only survive if they are passed back in.
                payment.fail(
                    info={**info_data, "state": state.value if state else None}
                )
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
                **{
                    k: v
                    for k, v in (payment.info_data or {}).items()
                    if k != PENDING_TRANSACTION_ID_KEY
                },
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
                **{
                    k: v
                    for k, v in (payment.info_data or {}).items()
                    if k != PENDING_TRANSACTION_ID_KEY
                },
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

    def _installment_token_client(self, plan: Any) -> PostFinanceClient:
        """
        Create a client for the space an installment plan's token lives in.

        A token can only be charged in the space that created it. The space
        is recorded with the token, so a plan keeps working even if the
        event's test mode is switched after the first payment.
        """
        token_data = plan.payment_token or {}
        recorded_space_id = token_data.get(SPACE_ID_KEY)
        if recorded_space_id:
            test_space_id = self.settings.get("test_space_id")
            if test_space_id and str(recorded_space_id) == str(test_space_id):
                return self._get_client_for_mode("test")
            space_id = self.settings.get("space_id")
            if space_id and str(recorded_space_id) == str(space_id):
                return self._get_client_for_mode("live")
        return self._get_client()

    def store_installment_token(self, payment: OrderPayment, transaction: Any) -> None:
        """
        Record the reusable token an interactive installment payment produced.

        That is a plan's first payment, and any later one the customer makes
        themselves to recover from a declined charge — where the whole point
        may be that they are switching to a card that works, so the new
        token replaces the old.

        Called from both paths that can settle such a payment — the customer
        returning from the payment page and the transaction webhook —
        because whichever gets there first is the only one that sees the
        transaction, and without a stored token every later installment of
        the plan fails.

        The currency the customer was actually charged in is snapshotted
        alongside the token: the automatic charges have no checkout to ask
        again, so they follow whatever the last interactive payment chose.
        """
        installment = scheduled_installment_for_payment(payment)
        if installment is None:
            return

        info_data = payment.info_data or {}
        if info_data.get(INSTALLMENT_CHARGE_KEY):
            # An automatic charge against the plan's own token. It cannot
            # have produced a new one, and a late webhook for it must not
            # write a token back onto a plan that has since been completed
            # or cancelled — pretix clears the token in both cases.
            return

        plan = plan_for_order(payment.order)
        if plan is None:
            logger.warning(
                "Installment payment %s has no plan to store a token on",
                payment.pk,
            )
            return

        token = getattr(transaction, "token", None)
        token_id = getattr(token, "id", None)
        if not token_id:
            logger.warning(
                "Installment payment %s succeeded but no token was created; "
                "installments after the first one cannot be charged",
                payment.pk,
            )
            return

        token_data: dict[str, Any] = {
            "token_id": token_id,
            "customer_id": getattr(token, "customer_id", None),
            "customer_email": getattr(token, "customer_email_address", None),
            SPACE_ID_KEY: info_data.get(SPACE_ID_KEY)
            or self._space_id_for_payment(payment),
        }
        token_data.update(
            {key: info_data[key] for key in FX_INFO_KEYS if info_data.get(key)}
        )
        # The first payment's charged amount describes that payment only;
        # every installment converts its own amount at the stored rate.
        token_data.pop("charged_amount", None)
        plan.store_payment_token(token_data)

        info_data[TOKEN_ID_KEY] = token_id
        payment.info_data = info_data
        payment.save(update_fields=["info"])

        logger.info(
            "Stored payment token %s for installment plan %s",
            token_id,
            plan.pk,
        )

    def _installment_fx(self, plan: Any) -> tuple[str, Decimal] | None:
        """
        Return the currency and rate an installment plan is charged at.

        ``None`` when the plan is charged in the event currency, which is
        the ordinary case.

        The rate is the one snapshotted with the token when the plan was
        sold, not today's setting: otherwise a rate change mid-plan would
        silently reprice the installments the customer has left.

        :raises PaymentException: If the plan is charged in a currency whose
            rate is no longer known, so no amount can be derived from it.
        """
        base_currency = str(plan.order.event.currency)
        token_data = plan.payment_token or {}
        charged_currency = token_data.get("charged_currency")
        if not charged_currency or charged_currency == base_currency:
            return None

        raw_rate = token_data.get("fx_rate")
        if raw_rate is None:
            # Plans predating the snapshot fall back to the configured rate,
            # but only while it still describes the same currency.
            config = self.alt_currency_config
            if config and config[0] == charged_currency:
                raw_rate = config[1]
        if raw_rate is None:
            raise self._unknown_rate_exception(str(charged_currency))

        rate = Decimal(str(raw_rate))
        if rate <= 0:
            raise self._unknown_rate_exception(str(charged_currency))

        return str(charged_currency), rate

    def installment_charge(self, plan: Any, installment: Any) -> tuple[str, Decimal]:
        """
        Return the currency and amount to charge for one installment.

        pretix schedules installments in the event currency; a plan sold in
        the alternative currency converts each share on its own, so the
        charges follow pretix's split rather than re-dividing a converted
        total.
        """
        amount = Decimal(str(installment.amount))
        fx = self._installment_fx(plan)
        if fx is None:
            return str(plan.order.event.currency), amount
        currency, rate = fx
        return currency, self._convert(amount, rate)

    @staticmethod
    def _transaction_info(transaction: Any) -> dict[str, Any]:
        """
        Describe a transaction the way the interactive payment paths do.

        Above all this carries the transaction state, which is what decides
        whether the payment can be refunded later.
        """
        if transaction is None:
            return {}
        state = getattr(transaction, "state", None)
        connector = getattr(transaction, "payment_connector_configuration", None)
        created_on = getattr(transaction, "created_on", None)
        return {
            "state": state.value if state else None,
            "payment_method": getattr(connector, "name", None) if connector else None,
            "created_on": str(created_on) if created_on else None,
        }

    @staticmethod
    def _fail_installment(
        installment: Any, payment: OrderPayment | None, reason: str, info: dict[str, Any]
    ) -> bool:
        """
        Record why an installment could not be charged.

        The reason goes on the installment, which is what pretix shows the
        customer and the organizer, and on the payment, which pretix marks
        failed with whatever we leave in ``info_data``. Being customer-facing,
        the reasons passed in here are translated — except where PostFinance
        supplies its own wording, which is passed through as it comes.
        """
        installment.failure_reason = reason
        installment.save(update_fields=["failure_reason"])
        if payment is not None:
            payment.info_data = {**info, "error": reason}
        return False

    def _previous_charge_attempt(
        self, payment: OrderPayment, installment: Any
    ) -> tuple[int, Any] | None:
        """
        Find the transaction a previous attempt at this installment left behind.

        Returns ``(transaction_id, space_id)``, or ``None`` if this is the first
        attempt. The space is whatever was recorded in ``info_data``, which is
        untyped JSON — the callers only pass it back through, never arithmetic.

        pretix builds a fresh ``OrderPayment`` for every attempt and re-points the
        installment at it, so the earlier attempts are only reachable through the
        order. They are recognised by the installment id recorded on them.
        """
        candidates = payment.order.payments.filter(
            provider=self.identifier,
            state__in=(
                OrderPayment.PAYMENT_STATE_CREATED,
                OrderPayment.PAYMENT_STATE_FAILED,
            ),
        ).exclude(pk=payment.pk).order_by("-pk")

        for candidate in candidates:
            info = candidate.info_data or {}
            if info.get(INSTALLMENT_ID_KEY) != installment.pk:
                continue
            transaction_id = info.get("transaction_id")
            if transaction_id:
                return int(transaction_id), info.get(SPACE_ID_KEY)
        return None

    def _settled_by_previous_attempt(
        self, payment: OrderPayment, installment: Any, client: PostFinanceClient
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        """
        Decide whether a previous attempt at this installment already took the money.

        A charge is two calls — create the transaction, then process it — and the
        transaction id is recorded before the second one. So an attempt whose
        response was lost after the money moved leaves behind a transaction that
        PostFinance considers settled while pretix considers the attempt failed.
        Charging again without looking would take the money twice.

        Returns ``(handled, info, reason)``:

        * ``(True, info, None)`` — it settled. Adopt that transaction instead of
          creating another one; the money is already in.
        * ``(True, None, reason)`` — it is neither settled nor dead yet, or could
          not be checked. Charging now risks doubling, so report a failure and let
          the next scheduled run look again.
        * ``(False, None, None)`` — nothing in the way; charge normally.
        """
        previous = self._previous_charge_attempt(payment, installment)
        if previous is None:
            return (False, None, None)

        transaction_id, space_id = previous
        try:
            transaction = client.get_transaction(transaction_id)
        except PostFinanceError as e:
            logger.warning(
                "Could not check transaction %s from a previous attempt at "
                "installment %s: %s",
                transaction_id,
                installment.pk,
                e,
            )
            return (
                True,
                None,
                str(_("An earlier payment attempt could not be checked.")),
            )

        state = getattr(transaction, "state", None)
        if state in SUCCESS_STATES:
            logger.warning(
                "Installment %s was already paid by transaction %s from an earlier "
                "attempt; adopting it instead of charging again",
                installment.pk,
                transaction_id,
            )
            info: dict[str, Any] = {
                INSTALLMENT_CHARGE_KEY: True,
                INSTALLMENT_ID_KEY: installment.pk,
                "transaction_id": transaction_id,
            }
            if space_id is not None:
                info[SPACE_ID_KEY] = space_id
            info.update(self._transaction_info(transaction))
            return (True, info, None)

        if state in FAILURE_STATES:
            return (False, None, None)

        logger.info(
            "Transaction %s from an earlier attempt at installment %s is still in "
            "state %s; not charging again yet",
            transaction_id,
            installment.pk,
            state,
        )
        return (
            True,
            None,
            str(_("An earlier payment attempt is still being processed.")),
        )

    def execute_installment(
        self, plan: Any, installment: Any, payment: OrderPayment
    ) -> bool:
        """
        Charge a scheduled installment against the plan's stored token.

        Runs without the customer present, from pretix's periodic task, so
        it never raises: a failure is reported by returning ``False`` with a
        reason recorded on the installment and the payment, which pretix
        turns into a grace period and a notification.

        ``payment`` is an ``OrderPayment`` in state ``created`` that pretix
        has already built for this installment. It does not need saving —
        pretix persists ``info`` when it confirms or fails the payment — but
        what goes in it decides what can be done with the charge afterwards:
        without the transaction reference the payment cannot be refunded.

        :param plan: The InstallmentPlan holding the token to charge
        :param installment: The ScheduledInstallment to pay
        :param payment: The OrderPayment to record this charge against
        :return: True if the charge succeeded
        """
        # Marks the payment as an automatic charge rather than something the
        # customer paid on the payment page. `store_installment_token` reads
        # it to know a webhook for this transaction cannot carry a new token.
        info: dict[str, Any] = {
            INSTALLMENT_CHARGE_KEY: True,
            # Ties this attempt to the installment, so that if it dies without
            # telling us what happened, the next one can find its transaction.
            INSTALLMENT_ID_KEY: installment.pk,
        }
        try:
            token_data = plan.payment_token or {}
            token_id = token_data.get("token_id")
            if not token_id:
                logger.error(
                    "Cannot execute installment %s for order %s: no token_id in payment_token",
                    installment.pk,
                    plan.order.code,
                )
                return self._fail_installment(
                    installment,
                    payment,
                    str(_("No stored payment method is available for this plan.")),
                    info,
                )

            currency, amount = self.installment_charge(plan, installment)
            fx = self._installment_fx(plan)
            if fx is not None:
                # Snapshotted on the payment the same way an interactive
                # payment does it, so refunds of this installment convert
                # back at the rate it was actually charged at.
                info.update(
                    {
                        "fx_rate": str(fx[1]),
                        "fx_base_currency": str(plan.order.event.currency),
                        "charged_currency": currency,
                        "charged_amount": str(amount),
                    }
                )

            line_items = [
                LineItemCreate(
                    name=str(_("Installment {number} of {count} for order {code}")).format(
                        number=installment.installment_number,
                        count=plan.total_installments,
                        code=plan.order.code,
                    ),
                    quantity=1,
                    amountIncludingTax=float(amount),
                    type=LineItemType.PRODUCT,
                    uniqueId=f"installment-{plan.pk}-{installment.installment_number}",
                )
            ]

            client = self._installment_token_client(plan)

            # A charge is two calls, and the money moves on the second. An attempt
            # whose response was lost in between leaves a transaction PostFinance
            # considers settled and pretix considers failed, so ask what became of
            # the last one before creating another.
            handled, settled_info, reason = self._settled_by_previous_attempt(
                payment, installment, client
            )
            if handled:
                if settled_info is not None:
                    payment.info_data = settled_info
                    return True
                return self._fail_installment(
                    installment, payment, reason or "", info
                )

            transaction = client.create_transaction(
                currency=currency,
                line_items=line_items,
                merchant_reference=self._get_order_merchant_reference(
                    plan.order.code,
                    installment_number=installment.installment_number,
                ),
                token=token_id,
                customers_presence=CustomersPresence.NOT_PRESENT,
                customer_id=token_data.get("customer_id"),
                customer_email_address=token_data.get("customer_email"),
            )

            if not transaction.id:
                logger.error(
                    "PostFinance transaction missing ID for installment %s of order %s",
                    installment.pk,
                    plan.order.code,
                )
                return self._fail_installment(
                    installment,
                    payment,
                    str(_("PostFinance did not return a transaction reference.")),
                    info,
                )

            info.update(
                {
                    "transaction_id": transaction.id,
                    SPACE_ID_KEY: client.space_id,
                    TOKEN_ID_KEY: token_id,
                }
            )
            # Recorded before the charge is made: if the call dies after the
            # money moved, the payment still names the transaction to
            # reconcile and refund against.
            payment.info_data = info

            logger.info(
                "Created installment transaction %s for installment %s of order %s "
                "using token %s (%s %s)",
                transaction.id,
                installment.pk,
                plan.order.code,
                token_id,
                amount,
                currency,
            )

            charge = client.process_with_token(transaction.id)
            charge_state = charge.state
            info.update(self._transaction_info(getattr(charge, "transaction", None)))
            payment.info_data = info

            if charge_state == ChargeState.SUCCESSFUL:
                logger.info(
                    "Installment %s for order %s succeeded (PostFinance charge state: %s)",
                    installment.pk,
                    plan.order.code,
                    charge_state,
                )
                return True

            if charge_state == ChargeState.FAILED:
                failure_reason = None
                if charge.failure_reason:
                    failure_reason = charge.failure_reason.description
                logger.warning(
                    "Installment %s for order %s failed (PostFinance charge state: %s, "
                    "reason: %s)",
                    installment.pk,
                    plan.order.code,
                    charge_state,
                    failure_reason,
                )
                return self._fail_installment(
                    installment,
                    payment,
                    # PostFinance's own wording when it gives one; it is more
                    # use to the customer than anything we could substitute.
                    str(failure_reason or _("The payment was declined by PostFinance.")),
                    info,
                )

            logger.warning(
                "Installment %s for order %s is pending or incomplete "
                "(PostFinance charge state: %s)",
                installment.pk,
                plan.order.code,
                charge_state,
            )
            return self._fail_installment(
                installment,
                payment,
                str(
                    _("The payment did not complete (PostFinance state: {state}).")
                ).format(state=charge_state.value)
                if charge_state
                else str(_("The payment did not complete.")),
                info,
            )

        except PaymentException as e:
            logger.error(
                "Cannot charge installment %s for order %s: %s",
                installment.pk,
                plan.order.code,
                e,
            )
            return self._fail_installment(installment, payment, str(e), info)

        except PostFinanceError as e:
            logger.exception("PostFinance API error during installment execution: %s", e)
            return self._fail_installment(installment, payment, str(e), info)

        except Exception as e:
            logger.exception("Unexpected error during installment execution: %s", e)
            return self._fail_installment(installment, payment, str(e), info)


    def revoke_payment_token(self, plan: Any) -> None:
        """
        Delete the plan's token at PostFinance.

        Called when a plan is completed or cancelled, so a stored payment
        method cannot be charged again afterwards. Failing to reach
        PostFinance is logged rather than raised: pretix clears the token on
        its side either way, and a plan must not stay open because a cleanup
        call failed.

        :param plan: The InstallmentPlan holding the token to revoke
        """
        try:
            token_data = plan.payment_token or {}
            token_id = token_data.get("token_id")
            if not token_id:
                logger.info("No token to revoke for installment plan %s", plan.pk)
                return

            client = self._installment_token_client(plan)
            client.delete_token(token_id)

            logger.info(
                "Revoked payment token %s for installment plan %s",
                token_id,
                plan.pk,
            )

        except PostFinanceError as e:
            logger.warning(
                "Failed to revoke token for installment plan %s: %s",
                plan.pk,
                e,
            )

        except Exception as e:
            logger.warning(
                "Unexpected error revoking token for installment plan %s: %s",
                plan.pk,
                e,
            )


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

            # PostFinance will not execute a second refund carrying an
            # external ID it has already seen, which is what makes retrying
            # one safe. Event slugs are only unique within an organizer, so
            # the organizer has to be part of the ID: without it two
            # organizers sharing a space can produce the same one, and the
            # second refund is quietly skipped rather than rejected.
            external_id = (
                f"pretix-{self.event.organizer.slug}-{self.event.slug}"
                f"-{refund.order.code}-R-{refund.local_id}"
            )
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

    def _refund_charged_currency(self, refund: OrderRefund) -> str | None:
        """
        The currency a refund is booked in, when it is not the event's.

        Returns ``None`` for a refund drawn on a payment charged in the event
        currency, where pretix already shows the only amount there is.
        """
        payment = cast("OrderPayment | None", refund.payment)
        if payment is None:
            return None
        charged_currency = str((payment.info_data or {}).get("charged_currency") or "")
        if not charged_currency or charged_currency == self.event.currency:
            return None
        return charged_currency

    def refund_control_render(self, request: HttpRequest, refund: OrderRefund) -> str:
        """
        Render refund details for the admin order view.

        pretix keeps its books in the event currency, so a refund drawn on a
        transaction charged in the alternative currency shows an amount that
        is not the one PostFinance moved — and the order page and the
        PostFinance dashboard end up showing two different numbers with
        nothing linking them. Showing what was actually booked, next to the
        transaction it was booked against, is what makes the two reconcile.
        This matters most after a cancellation that keeps a fee: the fee is
        set in the event currency, but what the customer really keeps paying
        is the charge minus this refund.
        """
        info_data = refund.info_data or {}

        charged_amount = None
        charged_currency = self._refund_charged_currency(refund)
        booked = info_data.get("amount")
        if charged_currency and booked is not None:
            charged_amount = money_filter(Decimal(str(booked)), charged_currency)

        payment = cast("OrderPayment | None", refund.payment)
        transaction_id = None
        dashboard_url = None
        if payment is not None:
            transaction_id = (payment.info_data or {}).get("transaction_id")
            space_id = info_data.get(SPACE_ID_KEY) or self._space_id_for_payment(payment)
            if transaction_id and space_id:
                dashboard_url = (
                    f"https://checkout.postfinance.ch/s/{space_id}"
                    f"/payment/transaction/view/{transaction_id}"
                )

        error_suggestion = None
        error_status = info_data.get("error_status_code")
        if error_status and int(error_status) in ERROR_STATUS_MESSAGES:
            error_suggestion = ERROR_STATUS_MESSAGES[int(error_status)]

        template = get_template("pretixplugins/postfinance/control_refund.html")
        ctx = {
            "request": request,
            "event": self.event,
            "refund": refund,
            "refund_info": info_data,
            "charged_amount": charged_amount,
            "transaction_id": transaction_id,
            "dashboard_url": dashboard_url,
            "error_suggestion": error_suggestion,
        }
        return template.render(ctx)

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

        What describes the money movement itself is kept: it is not personal
        data on its own, and refunding through PostFinance after an event was
        shredded still depends on it. The FX snapshot in particular has to
        survive — without it a refund of a payment charged in the alternative
        currency looks like a plain event-currency payment, and the event
        currency amount would be sent to a transaction booked in another
        currency. The token ID is deliberately not kept: it is a handle to the
        customer's stored payment method, and the plan revokes it anyway.
        """
        if not isinstance(obj, (OrderPayment, OrderRefund)):
            return

        info_data = obj.info_data or {}
        if isinstance(obj, OrderPayment):
            kept: dict[str, Any] = {
                "transaction_id": info_data.get("transaction_id"),
                SPACE_ID_KEY: info_data.get(SPACE_ID_KEY),
                "state": info_data.get("state"),
            }
            # Marks the payment as an automatic charge, which keeps a late
            # webhook for it from writing a token back onto its plan.
            if info_data.get(INSTALLMENT_CHARGE_KEY):
                kept[INSTALLMENT_CHARGE_KEY] = True
            kept.update({key: info_data[key] for key in FX_INFO_KEYS if info_data.get(key)})
        else:
            # The booked amount is what the next refund on the same
            # transaction draws down, so losing it would make that refund
            # fall back to re-converting and drift by a cent.
            kept = {
                "refund_id": info_data.get("refund_id"),
                SPACE_ID_KEY: info_data.get(SPACE_ID_KEY),
                "state": info_data.get("state"),
                "amount": info_data.get("amount"),
            }

        obj.info_data = {**kept, "_shredded": True}
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
        return _("{name} (production space)").format(name=self._base_public_name)

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
