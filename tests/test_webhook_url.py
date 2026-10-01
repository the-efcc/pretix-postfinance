from __future__ import annotations

import pytest

from pretix_postfinance.payment import (
    WEBHOOK_BASE_URL_OPTION,
    WEBHOOK_BASE_URL_SECTION,
    PostFinancePaymentProvider,
    webhook_url,
)

# EnvOrParserConfig reads the environment before pretix.cfg, so setting this
# is equivalent to writing the option into the config file.
ENV_KEY = f"PRETIX_{WEBHOOK_BASE_URL_SECTION.upper()}_{WEBHOOK_BASE_URL_OPTION.upper()}"


@pytest.fixture
def override(monkeypatch):
    def _set(value):
        monkeypatch.setenv(ENV_KEY, value)

    return _set


def test_defaults_to_the_instance_site_url():
    # SITE_URL is http://example.com in tests/settings.py
    assert webhook_url() == "http://example.com/_postfinance/webhook/"


def test_override_replaces_the_host(override):
    override("https://pretix-staging.example.org")

    assert webhook_url() == "https://pretix-staging.example.org/_postfinance/webhook/"


def test_override_keeps_the_path_from_the_url_config(override):
    """The path is never taken from the override, so the two cannot drift."""
    override("https://pretix-staging.example.org/somewhere/else/")

    assert webhook_url() == "https://pretix-staging.example.org/_postfinance/webhook/"


def test_override_accepts_a_bare_host(override):
    override("pretix-staging.example.org")

    assert webhook_url() == "https://pretix-staging.example.org/_postfinance/webhook/"


def test_override_keeps_an_explicit_port(override):
    override("https://pretix-staging.example.org:8443")

    assert webhook_url() == "https://pretix-staging.example.org:8443/_postfinance/webhook/"


@pytest.mark.parametrize("value", ["", "   ", "https://"])
def test_unusable_override_falls_back_to_the_site_url(override, value):
    override(value)

    assert webhook_url() == "http://example.com/_postfinance/webhook/"


@pytest.mark.django_db
def test_settings_page_shows_the_overridden_url(event, override):
    """The admin has to see the URL that "Setup webhooks" will register."""
    from django.test import RequestFactory

    override("https://pretix-staging.example.org")
    provider = PostFinancePaymentProvider(event)

    html = provider.settings_content_render(RequestFactory().get("/"))

    assert "https://pretix-staging.example.org/_postfinance/webhook/" in html


