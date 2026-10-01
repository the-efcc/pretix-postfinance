"""
Views for PostFinance payment plugin.

Handles webhook callbacks and admin settings actions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.base.permissions import AnyPermissionOf
from pretix.control.permissions import EventPermissionRequiredMixin

from ._types import PretixHttpRequest
from .api import (
    REFUND_ENTITY_ID,
    TRANSACTION_ENTITY_ID,
    PostFinanceClient,
    PostFinanceError,
)
from .payment import (
    FAILURE_STATES,
    PROD_PROVIDER_IDENTIFIER,
    PROVIDER_IDENTIFIERS,
    SPACE_ID_KEY,
    SUCCESS_STATES,
    webhook_url,
)


def _validate_mode(raw: str | None) -> str | None:
    """Return the mode if valid ('live' or 'test'), else None."""
    if raw in ("live", "test"):
        return raw
    return None

logger = logging.getLogger(__name__)

WEBHOOK_STATUS_NOT_FOUND = "not_found"
WEBHOOK_STATUS_NO_CLIENT = "no_client"
WEBHOOK_STATUS_API_ERROR = "api_error"
WEBHOOK_STATUS_INTERNAL_ERROR = "internal_error"
WEBHOOK_STATUS_OK = "ok"


@csrf_exempt
@scopes_disabled()
def webhook(request: HttpRequest) -> HttpResponse:
    """
    Handle webhook notifications from PostFinance.

    PostFinance sends webhook notifications when transaction or refund states change.
    """
    if request.method != "POST":
        return HttpResponse(status=405)

    # Parse payload
    content_type = request.content_type or ""
    if "application/json" not in content_type:
        logger.warning("PostFinance webhook: invalid content type %s", content_type)
        return JsonResponse({"error": "Invalid content type"}, status=400)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("PostFinance webhook: invalid JSON - %s", e)
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"error": "Payload must be a JSON object"}, status=400)

    space_id = payload.get("spaceId")
    entity_id = payload.get("entityId")

    if not space_id:
        logger.warning("PostFinance webhook: missing spaceId")
        return JsonResponse({"error": "Missing spaceId"}, status=400)

    logger.info(
        "PostFinance webhook: spaceId=%s, entityId=%s",
        space_id,
        entity_id,
    )

    signature_header = request.headers.get("X-Signature")

    # Security logging helper
    def _log_security_event(reason: str) -> None:
        """Log webhook signature failure as security event."""
        payload_hash = hashlib.sha256(request.body).hexdigest()
        client_ip = _get_client_ip(request)
        logger.error(
            "security.webhook.signature_failure: reason=%s, space_id=%s, entity_id=%s, "
            "client_ip=%s, payload_hash=%s",
            reason,
            space_id,
            entity_id,
            client_ip,
            payload_hash,
        )

    # Validate signature
    if not signature_header:
        # Signature is required but not present
        _log_security_event("missing_signature")
        return JsonResponse({"error": "Signature required"}, status=401)

    client = _get_client_for_space(space_id)
    if not client:
        # Without credentials for this space the signature cannot be checked.
        # Refuse rather than processing an unverified payload: a genuine
        # webhook for a misconfigured space is retried once the space is
        # configured, an unverifiable one must not take effect at all.
        _log_security_event("no_validation_credentials")
        return JsonResponse(
            {"error": "No credentials configured to validate this signature"}, status=401
        )

    try:
        if not client.is_webhook_signature_valid(
            signature_header=signature_header,
            content=request.body.decode("utf-8"),
        ):
            _log_security_event("invalid_signature")
            return JsonResponse({"error": "Invalid signature"}, status=401)
    except PostFinanceError as e:
        logger.error("PostFinance webhook: signature validation error - %s", e)
        # Transient API errors should return 502 so PostFinance retries
        if e.status_code and e.status_code >= 500:
            return JsonResponse(
                {"error": "Signature validation service unavailable"}, status=502
            )
        _log_security_event("validation_error")
        return JsonResponse({"error": "Signature validation error"}, status=401)

    # Process webhook and return appropriate HTTP status code:
    # - 200: Success or entity not found in our DB (legitimate "not ours" case)
    # - 500: Configuration error or internal error (retriable)
    # - 502: External API error (PostFinance API call failed, retriable)
    if entity_id:
        status = WEBHOOK_STATUS_NOT_FOUND
        for handler in _handlers_for_listener(payload.get("listenerEntityId")):
            status, _processed = handler(entity_id, space_id)
            if status != WEBHOOK_STATUS_NOT_FOUND:
                break

        if status == WEBHOOK_STATUS_NO_CLIENT:
            return JsonResponse(
                {"error": "No PostFinance client configured for this space"},
                status=500,
            )

        if status == WEBHOOK_STATUS_API_ERROR:
            return JsonResponse(
                {"error": "Failed to fetch entity from PostFinance API"},
                status=502,
            )

        if status == WEBHOOK_STATUS_INTERNAL_ERROR:
            return JsonResponse(
                {"error": "Internal error processing webhook"},
                status=500,
            )

    return HttpResponse(status=200)


def _get_client_ip(request: HttpRequest) -> str:
    """Extract client IP address, handling reverse proxy headers."""
    x_forwarded_for = request.headers.get("X-Forwarded-For")
    if x_forwarded_for:
        # Take the first IP in the chain (original client)
        return x_forwarded_for.split(",")[0].strip()
    remote_addr = request.META.get("REMOTE_ADDR")
    return str(remote_addr) if remote_addr else "unknown"


def _get_client_from_event(
    event: Any, mode: Literal["live", "test"]
) -> PostFinanceClient | None:
    """Create a PostFinanceClient from an event's settings for a given space."""
    try:
        es = event.settings
        prefix = "payment_postfinance_test_" if mode == "test" else "payment_postfinance_"
        space_id = es.get(f"{prefix}space_id")
        user_id = es.get(f"{prefix}user_id")
        auth_key = es.get(f"{prefix}auth_key")

        if not all([space_id, user_id, auth_key]):
            return None

        return PostFinanceClient(
            space_id=int(space_id),
            user_id=int(user_id),
            api_secret=str(auth_key),
        )
    except Exception as e:
        logger.debug("Could not create client from event %s: %s", event.slug, e)
        return None


def _mode_for_space(event: Any, space_id: int) -> Literal["live", "test"] | None:
    """
    Return which configured space of the event matches the given space ID.

    Returns "live" for the production space, "test" for the test space,
    or None if the space is not configured on this event.
    """
    live_space_id = event.settings.get("payment_postfinance_space_id")
    if live_space_id and str(live_space_id) == str(space_id):
        return "live"
    test_space_id = event.settings.get("payment_postfinance_test_space_id")
    if test_space_id and str(test_space_id) == str(space_id):
        return "test"
    return None


def _events_for_space(space_id: int) -> list[Event]:
    """
    Return events that may be configured with the given space.

    Looks the space up in the settings store first, so the lookup does not
    depend on how many events an installation has. Events that inherit their
    settings from the organizer have no row of their own, so a bounded scan
    of live and test mode events is appended as a fallback.
    """
    indexed = list(
        Event.objects.filter(
            _settings_objects__key__in=(
                "payment_postfinance_space_id",
                "payment_postfinance_test_space_id",
            ),
            _settings_objects__value=str(space_id),
        )
        .only("id", "slug")
        .distinct()
    )
    scanned = (
        Event.objects.filter(Q(live=True) | Q(testmode=True))
        .exclude(pk__in=[e.pk for e in indexed])
        .only("id", "slug")[:100]
    )
    return indexed + list(scanned)


def _get_client_for_space(space_id: int) -> PostFinanceClient | None:
    """Find and return a PostFinanceClient for signature validation only."""
    for event in _events_for_space(space_id):
        try:
            mode = _mode_for_space(event, space_id)
            if mode:
                client = _get_client_from_event(event, mode)
                if client:
                    return client
                # Space matches but its credentials are incomplete — another
                # event may still hold usable credentials for it.
                logger.debug(
                    "Event %s matches space %s but has no usable credentials",
                    event.slug,
                    space_id,
                )
        except Exception as e:
            logger.debug("Could not check event %s settings: %s", event.slug, e)

    return None


def _recorded_space_id(obj: Any) -> str | None:
    """Return the space recorded on a payment/refund when it was created."""
    value = (obj.info_data or {}).get(SPACE_ID_KEY)
    return str(value) if value else None


def _derived_space_id(obj: Any) -> str | None:
    """
    Return the space a payment/refund was most likely created in.

    Used for entities created before the space was recorded on them: the
    production space provider always uses the production space, and the main
    provider used the test space if the order was in test mode and test
    credentials were configured.
    """
    settings = obj.order.event.settings
    live_space_id = settings.get("payment_postfinance_space_id")

    if obj.provider == PROD_PROVIDER_IDENTIFIER:
        return live_space_id

    if obj.order.testmode:
        test_space_id = settings.get("payment_postfinance_test_space_id")
        has_test_credentials = all(
            [
                test_space_id,
                settings.get("payment_postfinance_test_user_id"),
                settings.get("payment_postfinance_test_auth_key"),
            ]
        )
        if has_test_credentials:
            return test_space_id

    return live_space_id


def _find_entity(queryset: QuerySet, id_key: str, entity_id: int, space_id: int) -> Any | None:
    """
    Find the payment or refund an incoming webhook is about.

    PostFinance entity IDs are only unique within a space, so the same ID can
    identify a different transaction in the production and in the test space.
    Matching on the ID alone can therefore pick the wrong entity — for example
    confirming a live payment from a test space webhook — so the space has to
    match as well.
    """
    other_space_matches = 0

    for obj in queryset.filter(info__icontains=str(entity_id)):
        info_data = obj.info_data or {}
        if str(info_data.get(id_key)) != str(entity_id):
            continue

        expected_space_id = _recorded_space_id(obj) or _derived_space_id(obj)
        if expected_space_id and str(expected_space_id) == str(space_id):
            return obj
        other_space_matches += 1

    if other_space_matches:
        logger.warning(
            "PostFinance webhook: ignoring %s %s from space %s, "
            "%s local entity/entities with that ID belong to another space",
            id_key,
            entity_id,
            space_id,
            other_space_matches,
        )

    return None


def _client_for_entity(
    obj: Any, space_id: int, entity_id: int, label: str
) -> tuple[PostFinanceClient | None, str]:
    """
    Create a client for the space a webhook about this entity came from.

    Returns a tuple of (client, status); the client is None if the event's
    configuration cannot serve the space the webhook came from.
    """
    event = obj.order.event

    mode = _mode_for_space(event, space_id)
    if mode is None:
        logger.error(
            "PostFinance webhook: space %s is not configured for event %s, %s=%s",
            space_id,
            event.slug,
            label,
            entity_id,
        )
        return (None, WEBHOOK_STATUS_NO_CLIENT)

    client = _get_client_from_event(event, mode)
    if client is None:
        logger.error(
            "PostFinance webhook: no %s client configured for event %s, %s=%s",
            mode,
            event.slug,
            label,
            entity_id,
        )
        return (None, WEBHOOK_STATUS_NO_CLIENT)

    return (client, WEBHOOK_STATUS_OK)


WebhookHandler = Callable[[int, int], "tuple[str, bool | None]"]


def _handlers_for_listener(listener_entity_id: Any) -> list[WebhookHandler]:
    """
    Return the webhook processors to try, in order.

    PostFinance names the type of entity a webhook is about in
    `listenerEntityId`. Transaction and refund IDs are separate sequences and
    can collide, so dispatching on that type avoids handing a refund webhook
    to the transaction processor (and vice versa). Payloads without a known
    type fall back to trying both.
    """
    try:
        entity_type = int(listener_entity_id)
    except (TypeError, ValueError):
        entity_type = 0

    if entity_type == TRANSACTION_ENTITY_ID:
        return [_process_transaction_webhook]
    if entity_type == REFUND_ENTITY_ID:
        return [_process_refund_webhook]
    return [_process_transaction_webhook, _process_refund_webhook]


def _process_transaction_webhook(entity_id: int, space_id: int) -> tuple[str, bool | None]:
    """
    Process a transaction state update from webhook.

    Returns:
        tuple[str, bool | None]: A tuple of (status, processed) where:
            - status: WEBHOOK_STATUS_NOT_FOUND (entity not in our DB),
                      WEBHOOK_STATUS_NO_CLIENT (configuration error),
                      WEBHOOK_STATUS_API_ERROR (PostFinance API failed),
                      WEBHOOK_STATUS_OK (processed successfully)
            - processed: True if state changed, False if no change, None if not applicable
    """
    payment = _find_entity(
        OrderPayment.objects.filter(provider__in=PROVIDER_IDENTIFIERS),
        "transaction_id",
        entity_id,
        space_id,
    )

    if not payment:
        # Entity not found in our database - this webhook isn't for us
        return (WEBHOOK_STATUS_NOT_FOUND, None)

    # Get client from the payment's event settings (avoids O(N) event scan)
    client, status = _client_for_entity(payment, space_id, entity_id, "transaction")
    if client is None:
        return (status, None)

    try:
        transaction = client.get_transaction(int(entity_id))
    except PostFinanceError as e:
        # External API error - PostFinance API call failed
        logger.error(
            "PostFinance webhook: failed to fetch transaction %s: %s (status=%s, code=%s)",
            entity_id,
            e.message,
            e.status_code,
            e.error_code,
        )
        return (WEBHOOK_STATUS_API_ERROR, None)

    transaction_state = transaction.state

    payment_method = None
    if transaction.payment_connector_configuration:
        payment_method = transaction.payment_connector_configuration.name

    info_data = payment.info_data or {}
    info_data.update(
        {
            "transaction_id": entity_id,
            # Record the space for payments predating it, so later webhooks
            # match on the space instead of falling back to deriving it
            SPACE_ID_KEY: space_id,
            "state": transaction_state.value if transaction_state else None,
            "payment_method": payment_method,
        }
    )
    payment.info_data = info_data
    payment.save(update_fields=["info"])

    payment.order.log_action(
        "pretix_postfinance.webhook",
        data={
            "transaction_id": entity_id,
            "state": transaction_state.value if transaction_state else None,
        },
    )

    if payment.state in (
        OrderPayment.PAYMENT_STATE_CONFIRMED,
        OrderPayment.PAYMENT_STATE_REFUNDED,
    ):
        return (WEBHOOK_STATUS_OK, False)

    if transaction_state in SUCCESS_STATES:
        try:
            payment.confirm()
            logger.info("PostFinance webhook: payment %s confirmed", payment.pk)
            return (WEBHOOK_STATUS_OK, True)
        except Exception as e:
            logger.exception("PostFinance webhook: error confirming payment %s: %s", payment.pk, e)
            return (WEBHOOK_STATUS_INTERNAL_ERROR, None)

    if transaction_state in FAILURE_STATES:
        try:
            payment.fail(info={"state": transaction_state.value if transaction_state else None})
            logger.info("PostFinance webhook: payment %s failed", payment.pk)
            return (WEBHOOK_STATUS_OK, True)
        except Exception as e:
            logger.exception("PostFinance webhook: error failing payment %s: %s", payment.pk, e)
            return (WEBHOOK_STATUS_INTERNAL_ERROR, None)

    # Handle pending/intermediate states
    if payment.state == OrderPayment.PAYMENT_STATE_CREATED:
        payment.state = OrderPayment.PAYMENT_STATE_PENDING
        payment.save(update_fields=["state"])
        logger.info("PostFinance webhook: payment %s set to pending", payment.pk)
        return (WEBHOOK_STATUS_OK, True)

    return (WEBHOOK_STATUS_OK, False)


def _process_refund_webhook(entity_id: int, space_id: int) -> tuple[str, bool | None]:
    """
    Process a refund state update from webhook.

    PostFinance sends webhooks for refund entities when refund state changes.
    This is triggered when a refund reaches SUCCESSFUL or FAILED state.

    Returns:
        tuple[str, bool | None]: A tuple of (status, processed) where:
            - status: "not_found" (entity not in our DB),
                      "no_client" (configuration error),
                      "api_error" (PostFinance API failed),
                      "ok" (processed successfully)
            - processed: True if state changed, False if no change, None if not applicable
    """
    refund = _find_entity(
        OrderRefund.objects.filter(provider__in=PROVIDER_IDENTIFIERS),
        "refund_id",
        entity_id,
        space_id,
    )

    if not refund:
        # Entity not found in our database - this webhook isn't for us
        return (WEBHOOK_STATUS_NOT_FOUND, None)

    # Get client from the refund's event settings (avoids O(N) event scan)
    client, status = _client_for_entity(refund, space_id, entity_id, "refund")
    if client is None:
        return (status, None)

    try:
        pf_refund = client.get_refund(int(entity_id))
    except PostFinanceError as e:
        # External API error - PostFinance API call failed
        logger.error(
            "PostFinance webhook: failed to fetch refund %s: %s (status=%s, code=%s)",
            entity_id,
            e.message,
            e.status_code,
            e.error_code,
        )
        # Store error details in refund.info for admin visibility
        info_data = refund.info_data or {}
        info_data.update(
            {
                "error": str(e),
                "error_code": e.error_code,
                "error_status_code": e.status_code,
            }
        )
        refund.info = json.dumps(info_data)
        refund.save(update_fields=["info"])
        return (WEBHOOK_STATUS_API_ERROR, None)

    refund_state = pf_refund.state

    info_data = refund.info_data or {}
    info_data["refund_id"] = entity_id
    info_data[SPACE_ID_KEY] = space_id
    info_data["state"] = refund_state.value if refund_state else None
    refund.info = json.dumps(info_data)
    refund.save(update_fields=["info"])

    refund.order.log_action(
        "pretix_postfinance.refund.webhook",
        data={
            "refund_id": entity_id,
            "state": refund_state.value if refund_state else None,
        },
    )

    if refund_state and refund_state.value == "SUCCESSFUL":
        if refund.state != OrderRefund.REFUND_STATE_DONE:
            refund.done()
            logger.info("PostFinance webhook: refund %s marked done", refund.pk)
        return (WEBHOOK_STATUS_OK, True)

    if refund_state and refund_state.value == "FAILED":
        if refund.state not in (OrderRefund.REFUND_STATE_DONE, OrderRefund.REFUND_STATE_FAILED):
            refund.state = OrderRefund.REFUND_STATE_FAILED
            refund.save(update_fields=["state"])
            refund.order.log_action(
                "pretix.event.order.refund.failed",
                {
                    "local_id": refund.local_id,
                    "provider": refund.provider,
                },
            )
            logger.info("PostFinance webhook: refund %s failed", refund.pk)
        return (WEBHOOK_STATUS_OK, True)

    return (WEBHOOK_STATUS_OK, False)


class PostFinanceTestConnectionView(EventPermissionRequiredMixin, View):
    """AJAX endpoint for testing PostFinance API connection."""

    permission = AnyPermissionOf("event.settings.payment:write", "event.settings.general:write")

    def post(self, request: PretixHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        providers = request.event.get_payment_providers()
        provider = providers.get("postfinance")

        if not provider:
            return JsonResponse(
                {
                    "success": False,
                    "message": str(_("PostFinance payment provider not found.")),
                }
            )

        mode = _validate_mode(request.POST.get("mode"))
        success, message = provider.test_connection(mode=mode)
        return JsonResponse({"success": success, "message": message})


class PostFinanceSetupWebhooksView(EventPermissionRequiredMixin, View):
    """AJAX endpoint for setting up PostFinance webhooks automatically."""

    permission = AnyPermissionOf("event.settings.payment:write", "event.settings.general:write")

    def post(self, request: PretixHttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        providers = request.event.get_payment_providers()
        provider = providers.get("postfinance")

        if not provider:
            return JsonResponse(
                {
                    "success": False,
                    "message": str(_("PostFinance payment provider not found.")),
                }
            )

        mode = _validate_mode(request.POST.get("mode")) or "live"
        success, message = provider.setup_webhooks(webhook_url(), mode=mode)
        return JsonResponse({"success": success, "message": message})
