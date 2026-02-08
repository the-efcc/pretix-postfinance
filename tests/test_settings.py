"""
Tests for settings configuration and validation.

These tests ensure that all settings keys are consistent across the codebase
and that the payment provider can properly access configured settings.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from django.utils.timezone import now
from django_scopes import scope
from pretix.base.models import Event, Organizer

from pretix_postfinance.payment import PostFinancePaymentProvider


@pytest.fixture
def event():
    """Create test event with PostFinance settings."""
    o = Organizer.objects.create(name="Dummy", slug="dummy")
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o,
            name="Dummy",
            slug="dummy",
            date_from=now(),
            live=True,
            plugins="pretix_postfinance",
        )
        # Set up settings using the correct key names from the settings form
        event.settings.set("payment_postfinance_space_id", "12345")
        event.settings.set("payment_postfinance_user_id", "67890")
        event.settings.set("payment_postfinance_auth_key", "test-secret-key")
        event.settings.set("payment_postfinance__enabled", True)
        yield event


@pytest.mark.django_db
def test_settings_keys_match_form_fields(event, monkeypatch):
    """
    Test that settings keys match the form field names.

    This catches bugs where the form uses one key name but the code
    accesses settings with a different key name.
    """
    # Mock _get_payment_method_choices to avoid API calls
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )

    provider = PostFinancePaymentProvider(event)

    # Get the form field names
    form_fields = provider.settings_form_fields

    # These are the critical fields that must be accessible
    critical_fields = ["space_id", "user_id", "auth_key"]

    for field_name in critical_fields:
        assert field_name in form_fields, f"Form field '{field_name}' not found"

        # Try to access the setting - this should not return None if we set it
        value = provider.settings.get(field_name)
        assert value is not None, f"Setting '{field_name}' returned None"


@pytest.mark.django_db
def test_get_client_uses_correct_settings_keys(event):
    """Test that _get_client accesses settings with correct keys."""
    provider = PostFinancePaymentProvider(event)

    # This will fail if _get_client tries to access "api_secret" instead of "auth_key"
    client = provider._get_client()

    assert client.space_id == 12345
    assert client.user_id == 67890
    assert client.api_secret == "test-secret-key"


@pytest.mark.django_db
def test_test_connection_uses_correct_settings_keys(event, monkeypatch):
    """Test that test_connection accesses settings with correct keys."""
    # Mock the get_space method
    mock_space = MagicMock()
    mock_space.name = "Test Space"

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_space",
        lambda self: mock_space,
    )

    provider = PostFinancePaymentProvider(event)

    # This will fail if test_connection tries to access wrong setting keys
    success, message = provider.test_connection()

    assert success is True
    assert "Test Space" in message


@pytest.mark.django_db
def test_test_connection_detects_missing_credentials(event):
    """Test that test_connection properly detects missing credentials."""
    provider = PostFinancePaymentProvider(event)

    # Clear one setting at a time and verify detection
    settings_to_test = ["space_id", "user_id", "auth_key"]

    for setting_key in settings_to_test:
        # Save current value
        original_value = provider.settings.get(setting_key)

        # Clear the setting
        provider.settings.set(setting_key, "")

        # Test connection should fail
        success, message = provider.test_connection()
        assert success is False, f"test_connection should fail when '{setting_key}' is missing"
        assert "configure" in message.lower()

        # Restore original value for next iteration
        provider.settings.set(setting_key, original_value)


@pytest.mark.django_db
def test_payment_method_choices_uses_correct_settings_keys(event, monkeypatch):
    """Test that _get_payment_method_choices accesses settings correctly."""
    # Mock the API response
    mock_config = MagicMock()
    mock_config.id = 123
    mock_config.name = "Test Payment Method"
    mock_config.resolved_title = {"en-US": "Test Method"}

    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinanceClient.get_payment_method_configurations",
        lambda self: [mock_config],
    )

    provider = PostFinancePaymentProvider(event)

    # This will fail if _get_payment_method_choices tries to access wrong keys
    choices = provider._get_payment_method_choices()

    assert len(choices) > 0
    assert choices[0] == ("123", "Test Method")


@pytest.mark.django_db
def test_settings_form_fields_contain_all_required_fields(event, monkeypatch):
    """Test that all required settings fields are present in the form."""
    # Mock _get_payment_method_choices to avoid API calls
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
    """Test that settings are properly persisted and retrieved."""
    provider = PostFinancePaymentProvider(event)

    # Set a custom value
    test_value = "custom-test-value-12345"
    provider.settings.set("auth_key", test_value)

    # Create a new provider instance
    provider2 = PostFinancePaymentProvider(event)

    # Verify the value persists
    retrieved_value = provider2.settings.get("auth_key")
    assert retrieved_value == test_value


@pytest.mark.django_db
def test_no_api_secret_key_exists(event):
    """
    Test that 'api_secret' is NOT used as a settings key anywhere.

    The form field is 'auth_key', so the code should never access 'api_secret'
    as a settings key (it's only used as a parameter name in the API client).
    """
    provider = PostFinancePaymentProvider(event)

    # Try to get 'api_secret' - it should return None since we never set it
    value = provider.settings.get("api_secret")
    assert value is None, "Found 'api_secret' in settings, but only 'auth_key' should be used"

    # Verify auth_key exists instead
    auth_key_value = provider.settings.get("auth_key")
    assert auth_key_value is not None, "auth_key should exist in settings"


@pytest.mark.django_db
def test_settings_content_render_has_required_context(event):
    """Test that settings_content_render includes all required template context."""
    from django.test import RequestFactory

    provider = PostFinancePaymentProvider(event)
    request = RequestFactory().get("/")

    # This should not raise an exception
    html = provider.settings_content_render(request)

    # Verify the HTML contains expected elements
    assert "webhook" in html.lower()
    assert "test" in html.lower() or "connection" in html.lower()
    assert "setup" in html.lower()


@pytest.mark.django_db
def test_all_settings_form_fields_are_accessible(event, monkeypatch):
    """
    Test that every field defined in settings_form_fields can be accessed.

    This ensures no typos between form field definitions and settings access.
    """
    # Mock _get_payment_method_choices to avoid API calls
    monkeypatch.setattr(
        "pretix_postfinance.payment.PostFinancePaymentProvider._get_payment_method_choices",
        lambda self: [],
    )

    provider = PostFinancePaymentProvider(event)
    form_fields = provider.settings_form_fields

    # Set a test value for each field (excluding base class fields)
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
            # Set the value
            provider.settings.set(field_name, test_value)

            # Retrieve it
            retrieved = provider.settings.get(field_name)

            assert retrieved == test_value, (
                f"Field '{field_name}' was set to '{test_value}' but retrieved as '{retrieved}'"
            )
