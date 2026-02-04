# Development Guide

This document helps AI agents understand the pretix-postfinance project structure and development workflow.

## Project Overview

PostFinance Checkout payment plugin for pretix.

## Key Files

- **`pretix_postfinance/payment.py`**: Main payment provider (BasePaymentProvider subclass)
- **`pretix_postfinance/views.py`**: Admin views for capture/refund + webhook handler
- **`pretix_postfinance/api.py`**: PostFinance Checkout SDK wrapper
- **`pretix_postfinance/_types.py`**: Type definitions for pretix-specific types
- **`tests/`**: pytest test suite

## Architecture

### Payment Flow
1. User initiates payment -> `payment_form_render()`
2. Payment created via PostFinance API -> transaction ID stored in `info_data`
3. User redirected to PostFinance checkout
4. Webhook receives transaction state updates -> `_process_transaction_state()`
5. Payment marked as confirmed/failed in pretix

### Refund Flow
1. Admin initiates refund -> `PostFinanceRefundView`
2. Refund created via API -> refund ID stored
3. Webhook receives refund state updates -> `_process_refund_state()`
4. Refund history tracked in `info_data['refund_history']`

## Development Commands
```bash
# Lint
devenv shell -- uv run ruff check --fix

# Type check
devenv shell -- uv run mypy pretix_postfinance/

# Test
devenv shell -- uv run pytest tests/ -q -W ignore --cov=pretix_postfinance --cov-report=term-missing
```

## Important Conventions

2. **Type Hints**: Strict mypy with django-stubs, use `PretixHttpRequest` for views
3. **Payment Info Storage**: Use `payment.info_data` dict for transaction/refund metadata
4. **Error Handling**: Store `error_code` and `error_status_code` in info_data
5. **Import Sorting**: stdlib -> third-party -> local (enforced by ruff)

## Testing Strategy

- Unit tests for API client and utilities
- Mocked PostFinance SDK services
- Coverage reporting in CI with diff on PRs

## CI/CD

GitHub workflow runs on PRs:
- **test**: pytest with coverage (Python 3.9-3.14)
- **coverage-diff**: Shows coverage change in PR comments
- **typecheck**: mypy strict type checking
- **lint**: ruff linting

## Type System Notes

- Use `PretixHttpRequest` instead of `HttpRequest` for views that access `request.event`
- Django plugin configured in `pyproject.toml` with `django_settings_module = "tests.settings"`
- Ignore missing imports for `pretix.*` and `postfinancecheckout.*`
