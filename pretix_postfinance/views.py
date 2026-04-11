"""
Views for PostFinance payment plugin.

Handles webhook callbacks and admin settings actions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_scopes import scopes_disabled
from pretix.base.models import Event, OrderPayment, OrderRefund
from pretix.control.permissions import EventPermissionRequiredMixin
from pretix.helpers.urls import build_absolute_uri

from ._types import PretixHttpRequest
from .api import PostFinanceClient, PostFinanceError
from .payment import FAILURE_STATES, SUCCESS_STATES

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
    if signature_header:
        client = _get_client_for_space(space_id)
        if client:
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
    else:
        # Signature is required but not present
        _log_security_event("missing_signature")
        return JsonResponse({"error": "Signature required"}, status=401)

    # Process webhook and return appropriate HTTP status code:
    # - 200: Success or entity not found in our DB (legitimate "not ours" case)
    # - 500: Configuration error or internal error (retriable)
    # - 502: External API error (PostFinance API call failed, retriable)
    if entity_id:
        status, _ = _process_transaction_webhook(entity_id, space_id)

        if status == WEBHOOK_STATUS_NOT_FOUND:
            # Try refund processing if transaction not found
            status, _ = _process_refund_webhook(entity_id, space_id)

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


def _get_client_from_event(event: Any) -> PostFinanceClient | None:
    """Create a PostFinanceClient from an event's settings."""
    try:
        es = event.settings
        space_id = es.get("payment_postfinance_space_id")
        user_id = es.get("payment_postfinance_user_id")
        auth_key = es.get("payment_postfinance_auth_key")

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


def _get_client_for_space(space_id: int) -> PostFinanceClient | None:
    """Find and return a PostFinanceClient for signature validation only."""
    for event in Event.objects.filter(live=True).only("id", "slug")[:100]:
        try:
            event_space_id = event.settings.get("payment_postfinance_space_id")
            if str(event_space_id) == str(space_id):
                return _get_client_from_event(event)
        except Exception as e:
            logger.debug("Could not check event %s settings: %s", event.slug, e)

    return None


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
    payment = None
    for p in OrderPayment.objects.filter(
        provider="postfinance",
        info__icontains=str(entity_id),
    ):
        info_data = p.info_data or {}
        if str(info_data.get("transaction_id")) == str(entity_id):
            payment = p
            break

    if not payment:
        # Entity not found in our database - this webhook isn't for us
        return (WEBHOOK_STATUS_NOT_FOUND, None)

    # Get client from the payment's event settings (avoids O(N) event scan)
    client = _get_client_from_event(payment.order.event)
    if not client:
        logger.error(
            "PostFinance webhook: no client configured for event %s, transaction=%s",
            payment.order.event.slug,
            entity_id,
        )
        return (WEBHOOK_STATUS_NO_CLIENT, None)

    # Verify the space_id matches the payment's event configuration
    event_space_id = payment.order.event.settings.get("payment_postfinance_space_id")
    if str(event_space_id) != str(space_id):
        logger.warning(
            "PostFinance webhook: space_id mismatch for transaction %s "
            "(webhook: %s, event: %s)",
            entity_id,
            space_id,
            event_space_id,
        )
        return (WEBHOOK_STATUS_OK, False)

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

    payment.info_data = payment.info_data or {}
    payment.info_data.update(
        {
            "transaction_id": entity_id,
            "state": transaction_state.value if transaction_state else None,
            "payment_method": payment_method,
        }
    )
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
    refund = None
    for r in OrderRefund.objects.filter(
        provider="postfinance",
        info__icontains=str(entity_id),
    ):
        info_data = r.info_data or {}
        if str(info_data.get("refund_id")) == str(entity_id):
            refund = r
            break

    if not refund:
        # Entity not found in our database - this webhook isn't for us
        return (WEBHOOK_STATUS_NOT_FOUND, None)

    # Get client from the refund's event settings (avoids O(N) event scan)
    client = _get_client_from_event(refund.order.event)
    if not client:
        logger.error(
            "PostFinance webhook: no client configured for event %s, refund=%s",
            refund.order.event.slug,
            entity_id,
        )
        return (WEBHOOK_STATUS_NO_CLIENT, None)

    # Verify the space_id matches the refund's event configuration
    event_space_id = refund.order.event.settings.get("payment_postfinance_space_id")
    if str(event_space_id) != str(space_id):
        logger.warning(
            "PostFinance webhook: space_id mismatch for refund %s "
            "(webhook: %s, event: %s)",
            entity_id,
            space_id,
            event_space_id,
        )
        return (WEBHOOK_STATUS_OK, False)

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

    permission = "can_change_event_settings"

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

        success, message = provider.test_connection()
        return JsonResponse({"success": success, "message": message})


class PostFinanceSetupWebhooksView(EventPermissionRequiredMixin, View):
    """AJAX endpoint for setting up PostFinance webhooks automatically."""

    permission = "can_change_event_settings"

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

        space_id = provider.settings.get("space_id")
        user_id = provider.settings.get("user_id")
        auth_key = provider.settings.get("auth_key")

        if not all([space_id, user_id, auth_key]):
            return JsonResponse(
                {
                    "success": False,
                    "message": str(
                        _(
                            "Please configure Space ID, User ID, and Authentication Key before "
                            "setting up webhooks."
                        )
                    ),
                }
            )
        webhook_url = build_absolute_uri("plugins:pretix_postfinance:postfinance.webhook")

        try:
            client = PostFinanceClient(
                space_id=int(space_id),
                user_id=int(user_id),
                api_secret=str(auth_key),
            )
            result = client.setup_webhooks(webhook_url)

            created_transaction = result.get("created_transaction_listener", False)
            created_refund = result.get("created_refund_listener", False)

            if created_transaction and created_refund:
                message = _(
                    "Webhooks configured successfully! "
                    "Transaction and refund updates will be received automatically."
                )
            elif created_transaction:
                message = _(
                    "Transaction webhook configured. "
                    "Refund webhook was already set up."
                )
            elif created_refund:
                message = _(
                    "Refund webhook configured. "
                    "Transaction webhook was already set up."
                )
            else:
                message = _(
                    "Webhooks are already configured. "
                    "No changes were needed."
                )

            return JsonResponse(
                {
                    "success": True,
                    "message": str(message),
                    "details": result,
                }
            )
        except PostFinanceError as e:
            return JsonResponse(
                {
                    "success": False,
                    "message": str(_("Failed to setup webhooks: {error}").format(error=str(e))),
                }
            )
