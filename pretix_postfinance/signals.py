from __future__ import annotations

from typing import Any

from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import resolve
from django.utils.translation import gettext_lazy as _
from pretix.base.logentrytypes import OrderLogEntryType, log_entry_types
from pretix.base.signals import register_payment_providers
from pretix.control.signals import html_head


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
        return _("PostFinance refund failed: {error}.").format(error=error)


@receiver(register_payment_providers, dispatch_uid="payment_postfinance")
def register_payment_provider(sender: Any, **kwargs: Any) -> type[Any]:
    """
    Register the PostFinance payment provider with pretix.
    """
    from .payment import PostFinancePaymentProvider

    return PostFinancePaymentProvider


@receiver(html_head, dispatch_uid="postfinance_control_html_head")
def control_html_head(sender: Any, request: Any, **kwargs: Any) -> str:
    """
    Inject PostFinance JavaScript into control panel pages.
    """
    url = resolve(request.path_info)
    # Only load on payment settings page
    if url.url_name and "settings" in url.url_name:
        template = get_template("pretixplugins/postfinance/control_head.html")
        return template.render()
    return ""
