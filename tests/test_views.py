from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pretix.base.models import Team, User

from pretix_postfinance.api import PostFinanceError


@pytest.fixture
def authenticated_client(client, event):
    user = User.objects.create_user("dummy@dummy.dummy", "dummy")
    team = Team.objects.create(
        organizer=event.organizer,
        limit_event_permissions={
            "event.settings.payment:write": True,
        },
    )
    team.members.add(user)
    team.limit_events.add(event)
    client.force_login(user)
    return client


class TestTestConnectionView:
    @pytest.mark.django_db
    def test_connection_success(self, authenticated_client, event, monkeypatch):
        client = authenticated_client

        mock_space = MagicMock()
        mock_space.name = "Test Space"

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_space",
            lambda self: mock_space,
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Test Space" in data["message"]

    @pytest.mark.django_db
    def test_connection_auth_error(self, authenticated_client, event, monkeypatch):
        client = authenticated_client

        def get_space_error():
            raise PostFinanceError("Unauthorized", status_code=401)

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinanceClient.get_space",
            lambda self: get_space_error(),
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "Authentication" in data["message"] or "failed" in data["message"].lower()

    @pytest.mark.django_db
    def test_connection_requires_login(self, authenticated_client, event):
        client = authenticated_client
        client.logout()

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url)

        # Should redirect to login
        assert response.status_code in (302, 403)

    @pytest.mark.django_db
    def test_connection_passes_mode_to_provider(
        self, authenticated_client, event, monkeypatch
    ):
        client = authenticated_client
        captured = {}

        def fake_test(self, mode=None):
            captured["mode"] = mode
            return True, "ok"

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinancePaymentProvider.test_connection",
            fake_test,
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/test-connection/"
        response = client.post(url, data={"mode": "test"})

        assert response.status_code == 200
        assert captured["mode"] == "test"


class TestSetupWebhooksView:
    @pytest.mark.django_db
    def test_setup_webhooks_passes_mode_to_provider(
        self, authenticated_client, event, monkeypatch
    ):
        client = authenticated_client
        captured = {}

        def fake_setup(self, webhook_url, mode):
            captured["mode"] = mode
            captured["webhook_url"] = webhook_url
            return True, "ok"

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinancePaymentProvider.setup_webhooks",
            fake_setup,
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/setup-webhooks/"
        response = client.post(url, data={"mode": "test"})

        assert response.status_code == 200
        assert captured["mode"] == "test"
        assert "webhook" in captured["webhook_url"]

    @pytest.mark.django_db
    def test_setup_webhooks_defaults_to_live_when_mode_invalid(
        self, authenticated_client, event, monkeypatch
    ):
        client = authenticated_client
        captured = {}

        def fake_setup(self, webhook_url, mode):
            captured["mode"] = mode
            return True, "ok"

        monkeypatch.setattr(
            "pretix_postfinance.payment.PostFinancePaymentProvider.setup_webhooks",
            fake_setup,
        )

        url = f"/control/event/{event.organizer.slug}/{event.slug}/postfinance/setup-webhooks/"
        response = client.post(url, data={"mode": "garbage"})

        assert response.status_code == 200
        assert captured["mode"] == "live"
