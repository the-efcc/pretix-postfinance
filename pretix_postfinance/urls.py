"""
URL configuration for PostFinance payment plugin.
"""


from django.urls import include, path, re_path

from . import views

urlpatterns = [
    path("_postfinance/webhook/", views.webhook, name="postfinance.webhook"),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/postfinance/test-connection/$",
        views.PostFinanceTestConnectionView.as_view(),
        name="postfinance.test_connection",
    ),
    re_path(
        r"^control/event/(?P<organizer>[^/]+)/(?P<event>[^/]+)/postfinance/setup-webhooks/$",
        views.PostFinanceSetupWebhooksView.as_view(),
        name="postfinance.setup_webhooks",
    ),
]

# Mounted under the event's own URL, with `request.event` set.
RETURN_PATH = r"(?P<order>[^/]+)/(?P<hash>[^/]+)/(?P<payment>[0-9]+)/$"

event_patterns = [
    re_path(
        r"^postfinance/",
        include(
            [
                re_path(
                    rf"^return/{RETURN_PATH}",
                    views.PostFinanceReturnView.as_view(),
                    name="return",
                ),
                re_path(
                    rf"^failed/{RETURN_PATH}",
                    views.PostFinanceReturnView.as_view(failed=True),
                    name="failed",
                ),
            ]
        ),
    ),
]
