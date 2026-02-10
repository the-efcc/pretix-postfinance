from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pretix_postfinance.payment import PostFinancePaymentProvider


@pytest.mark.django_db
def test_settings_keys_match_form_fields(event, monkeypatch):
    """
    Catches bugs where the form uses one key name but the code
    accesses settings with a different key name.
    """
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )

    provider = PostFinancePaymentProvider(event)

    form_fields = provider.settings_form_fields

    critical_fields = ["space_id", "user_id", "auth_key"]

    for field_name in critical_fields:
        assert field_name in form_fields, f"Form field '{field_name}' not found"

        value = provider.settings.get(field_name)
        assert value is not None, f"Setting '{field_name}' returned None"


@pytest.mark.django_db
def test_get_client_uses_correct_settings_keys(event):
    provider = PostFinancePaymentProvider(event)

    client = provider._get_client()

    assert client.space_id == 12345
    assert client.user_id == 67890
    assert client.api_secret == "test-secret"


@pytest.mark.django_db
def test_test_connection_uses_correct_settings_keys(event, monkeypatch):
    mock_space = MagicMock()
    mock_space.name = "Test Space"

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: mock_space,
    )

    provider = PostFinancePaymentProvider(event)

    success, message = provider.test_connection()

    assert success is True
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_detects_missing_credentials(event):
    provider = PostFinancePaymentProvider(event)

    settings_to_test = ["space_id", "user_id", "auth_key"]

    for setting_key in settings_to_test:
        original_value = provider.settings.get(setting_key)

        provider.settings.set(setting_key, "")

        success, message = provider.test_connection()
        assert success is False, f"test_connection should fail when '{setting_key}' is missing"
        assert "configure" in message.lower()

        provider.settings.set(setting_key, original_value)


@pytest.mark.django_db
def test_payment_method_choices_uses_correct_settings_keys(event, monkeypatch):
    mock_config = MagicMock()
    mock_config.id = 123
    mock_config.name = "Test Payment Method"
    mock_config.resolved_title = {"en-US": "Test Method"}

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_method_configurations",
        lambda self: [mock_config],
    )

    provider = PostFinancePaymentProvider(event)

    choices = provider._get_payment_method_choices()

    assert len(choices) > 0
    assert choices[0] == ("123", "Test Method")


@pytest.mark.django_db
def test_settings_form_fields_contain_all_required_fields(event, monkeypatch):
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )

    provider = PostFinancePaymentProvider(event)
    form_fields = provider.settings_form_fields

    required_fields = [
        "space_id",
        "user_id",
        "auth_key",
        "public_name",
        "description",
        "allowed_payment_methods",
    ]

    for field_name in required_fields:
        assert field_name in form_fields, f"Required field '{field_name}' missing from form"


@pytest.mark.django_db
def test_settings_persistence(event):
    provider = PostFinancePaymentProvider(event)

    test_value = "custom-test-value-12345"
    provider.settings.set("auth_key", test_value)

    provider2 = PostFinancePaymentProvider(event)

    retrieved_value = provider2.settings.get("auth_key")
    assert retrieved_value == test_value


@pytest.mark.django_db
def test_no_api_secret_key_exists(event):
    """
    The form field is 'auth_key', so the code should never access 'api_secret'
    as a settings key (it's only used as a parameter name in the API client).
    """
    provider = PostFinancePaymentProvider(event)

    value = provider.settings.get("api_secret")
    assert value is None, "Found 'api_secret' in settings, but only 'auth_key' should be used"

    auth_key_value = provider.settings.get("auth_key")
    assert auth_key_value is not None, "auth_key should exist in settings"


@pytest.mark.django_db
def test_settings_content_render_has_required_context(event):
    from django.test import RequestFactory

    provider = PostFinancePaymentProvider(event)
    request = RequestFactory().get("/")

    html = provider.settings_content_render(request)

    assert "webhook" in html.lower()
    assert "test" in html.lower() or "connection" in html.lower()
    assert "setup" in html.lower()


@pytest.mark.django_db
def test_all_settings_form_fields_are_accessible(event, monkeypatch):
    """
    Ensures no typos between form field definitions and settings access.
    """
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )

    provider = PostFinancePaymentProvider(event)
    form_fields = provider.settings_form_fields

    test_fields = {
        "space_id": "99999",
        "user_id": "88888",
        "auth_key": "test-key-value",
        "public_name": "Test Name",
        "description": "Test Description",
        "capture_mode": "immediate",
    }

    for field_name, test_value in test_fields.items():
        if field_name in form_fields:
            provider.settings.set(field_name, test_value)

            retrieved = provider.settings.get(field_name)

            assert retrieved == test_value, (
                f"Field '{field_name}' was set to '{test_value}' but retrieved as '{retrieved}'"
            )
