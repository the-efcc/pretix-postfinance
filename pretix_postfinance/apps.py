from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2026.3 or above to run this plugin!") from None


class PluginApp(PluginConfig):
    default = True
    name = "pretix_postfinance"
    verbose_name = "PostFinance"

    class PretixPluginMeta:
        name = _("PostFinance")
        author = "Sweenu"
        description = _("PostFinance Checkout payment plugin for pretix")
        visible = True
        picture = "pretix_postfinance/pf_logo.svg"
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=2026.3.0"
        settings_links = (
            (
                (_("Payment"), _("PostFinance")),
                "control:event.settings.payment.provider",
                {"provider": "postfinance"},
            ),
        )

    def ready(self) -> None:
        from . import signals  # noqa: F401
