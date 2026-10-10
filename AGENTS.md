# Development Guide

This document helps AI agents understand the pretix-postfinance project structure and development workflow.

## Project Overview

PostFinance Checkout payment plugin for pretix.

## Key Files

- **`pretix_postfinance/payment.py`**: Main payment provider (BasePaymentProvider subclass)
- **`pretix_postfinance/views.py`**: Admin views for capture/refund, webhook handler, and the pages PostFinance sends the customer back to
- **`pretix_postfinance/api.py`**: PostFinance Checkout SDK wrapper
- **`pretix_postfinance/_types.py`**: Type definitions for pretix-specific types
- **`pretix_postfinance/locale/`**: Message catalogs (de, fr, it, es)
- **`tests/`**: pytest test suite

## Architecture

### Payment Flow
1. User initiates payment -> `payment_form_render()`
2. Payment created via PostFinance API -> transaction ID stored in `info_data`
3. User redirected to PostFinance checkout
4. Webhook receives transaction state updates -> `_process_transaction_webhook()`
5. Payment marked as confirmed/failed in pretix
6. User sent back to `PostFinanceReturnView` (`postfinance/return/` or
   `postfinance/failed/`), which calls `sync_payment()`. Not pretix's
   `pay.complete`/`pay`: those reject any payment no longer `created`, and the
   webhook in step 4 usually gets there before the customer does.

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
devenv shell -- uv run ty check pretix_postfinance/

# Test
devenv shell -- uv run pytest tests/ -q -W ignore --cov=pretix_postfinance --cov-report=term-missing
```

### Updating translations

After adding or changing a user-facing string, re-extract both domains and
translate the new entries (see README for the full workflow):

```bash
cd pretix_postfinance
uv run django-admin makemessages -l de -l fr -l it -l es --no-obsolete
uv run django-admin makemessages -d djangojs -l de -l fr -l it -l es --no-obsolete
```

Run these **without** `DJANGO_SETTINGS_MODULE`. Extraction does not need
settings — it scans the files under the current directory — and loading them
does active harm when pretix is installed from a checkout: `tests/settings.py`
inherits pretix's `LOCALE_PATHS`, `makemessages` treats every entry in it as
somewhere it owns a temporary `.pot`, and its cleanup step deletes
`django.pot` and `djangojs.pot` out of the pretix checkout. Those are real
tracked files there, so the damage lands in a repository you were not even
working in. Without settings the only locale directory in play is this
plugin's, and the catalogs produced are byte-identical.

Leave no entry empty or `#, fuzzy`, and verify with:

```bash
msgfmt --check --check-format --statistics -o /dev/null \
    pretix_postfinance/locale/*/LC_MESSAGES/*.po
```

The `.mo` files are gitignored — `pretix-plugin-build` compiles them.

### Testing against the pretix fork

The plugin supports two pretixes: the upstream release pinned in `uv.lock`,
and the EFCC fork (`the-efcc/pretix`, branch `efcc`), which is the only one
with installments. The installment tests skip themselves on upstream, so the
fork has to be installed to run them:

```bash
git clone --branch efcc https://github.com/the-efcc/pretix .pretix-fork
uv pip install -e ./.pretix-fork
uv run --no-sync pytest tests/ -q -W ignore   # nothing should skip
uv sync --all-extras                          # back to upstream pretix
```

CI runs both: the `test` job on upstream across Python 3.11-3.14, and the
`test (pretix fork)` job on the fork, which fails if any test skips.

## Important Conventions

1. **Type Hints**: use `PretixHttpRequest` for views
2. **Payment Info Storage**: Use `payment.info_data` dict for transaction/refund metadata
3. **Error Handling**: Store `error_code` and `error_status_code` in info_data
4. **Import Sorting**: stdlib -> third-party -> local (enforced by ruff)
5. **Translations**: every user-facing string is wrapped in `gettext_lazy` (or
   `{% trans %}`/`{% blocktrans %}` in templates, `gettext()` in JS) and
   translated into de, fr, it and es. `xgettext` does not look inside
   f-strings, so wrap the whole literal — `_("... {name} ...").format(...)`,
   never `f"... {_('...')} ..."`. Webhook JSON responses go to PostFinance,
   not to a person, and stay untranslated.

## Testing Strategy

- Unit tests for API client and utilities
- Mocked PostFinance SDK services
- Coverage reporting in CI with diff on PRs

## CI/CD

GitHub workflow runs on PRs:
- **test**: pytest with coverage on the pinned upstream pretix (Python 3.11-3.14)
- **test (pretix fork)**: the same suite against `the-efcc/pretix`, and fails if
  any test skips itself (see "Testing against the pretix fork" above)
- **coverage-diff**: writes the coverage change to the job summary
- **typecheck**: `ty check` (`[tool.ty]` in `pyproject.toml`; not mypy)
- **lint**: ruff linting

## Type System Notes

- Use `PretixHttpRequest` instead of `HttpRequest` for views that access `request.event`
- Django plugin configured in `pyproject.toml` with `django_settings_module = "tests.settings"`
- Ignore missing imports for `pretix.*` and `postfinancecheckout.*`
