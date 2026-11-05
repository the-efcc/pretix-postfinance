from __future__ import annotations

from typing import Any

from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import Resolver404, resolve
from django.utils.translation import gettext_lazy as _
from pretix.base.logentrytypes import OrderLogEntryType, log_entry_types
from pretix.base.signals import register_payment_providers
from pretix.control.signals import html_head

# The control panel view that renders one payment provider's settings, which
# is the only page carrying the buttons the script wires up.
PROVIDER_SETTINGS_URL_NAME = "event.settings.payment.provider"


@log_entry_types.new()
class PostFinanceWebhookLogEntryType(OrderLogEntryType):
    action_type = "pretix_postfinance.webhook"

    def display(self, logentry, data):
        state = data.get("state")
        return _("PostFinance webhook received (state: {state}).").format(state=state)


@log_entry_types.new()
class PostFinanceRefundWebhookLogEntryType(OrderLogEntryType):
    action_type = "pretix_postfinance.refund.webhook"

    def display(self, logentry, data):
        state = data.get("state")
        return _("PostFinance refund webhook received (state: {state}).").format(state=state)


@log_entry_types.new()
class PostFinanceRefundLogEntryType(OrderLogEntryType):
    action_type = "pretix_postfinance.refund"

    def display(self, logentry, data):
        amount = data.get("amount")
        return _("PostFinance refund issued (amount: {amount}).").format(amount=amount)


@log_entry_types.new()
class PostFinanceRefundFailedLogEntryType(OrderLogEntryType):
    action_type = "pretix_postfinance.refund.failed"

    def display(self, logentry, data):
        error = data.get("error")
        if error is None:
            # PostFinance accepted the request but reported the refund failed.
            return _("PostFinance refund failed (state: {state}).").format(
                state=data.get("state")
            )
        return _("PostFinance refund failed: {error}.").format(error=error)


@log_entry_types.new()
class PostFinanceRefundUnknownLogEntryType(OrderLogEntryType):
    action_type = "pretix_postfinance.refund.unknown"

    def display(self, logentry, data):
        return _(
            "PostFinance did not confirm whether the refund was created: {error}."
        ).format(error=data.get("error"))


@receiver(register_payment_providers, dispatch_uid="payment_postfinance")
def register_payment_provider(sender: Any, **kwargs: Any) -> list[type[Any]]:
    """
    Register the PostFinance payment providers with pretix.

    The production space provider is only offered during checkout while the
    event is in test mode, so the production space can be tested end-to-end.
    """
    from .payment import PostFinancePaymentProvider, PostFinanceProdSpacePaymentProvider

    return [PostFinancePaymentProvider, PostFinanceProdSpacePaymentProvider]


@receiver(html_head, dispatch_uid="postfinance_control_html_head")
def control_html_head(sender: Any, request: Any, **kwargs: Any) -> str:
    """
    Load the settings page JavaScript, on that page only.

    The script drives the "Test connection" and "Setup webhooks" buttons,
    which only `settings_content_render()` puts on the page, and only for the
    main provider — the production space provider shares its settings and
    renders none of its own. Matching every control URL whose name merely
    contains "settings" served it on pages with nothing to put it to use.
    """
    # Imported lazily, like the provider registration above: this module is
    # imported from `AppConfig.ready()`.
    from .payment import MAIN_PROVIDER_IDENTIFIER

    try:
        url = resolve(request.path_info)
    except Resolver404:
        return ""

    if (
        url.url_name == PROVIDER_SETTINGS_URL_NAME
        and url.kwargs.get("provider") == MAIN_PROVIDER_IDENTIFIER
    ):
        template = get_template("pretixplugins/postfinance/control_head.html")
        return template.render()
    return ""
