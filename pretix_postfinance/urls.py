"""
URL configuration for PostFinance payment plugin.
"""


from django.urls import path, re_path

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
