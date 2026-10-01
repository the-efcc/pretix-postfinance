# pretix-postfinance

PostFinance Checkout payment plugin for pretix.

## Installation

### PyPI

```bash
pip install pretix-postfinance
```

### NixOS

For NixOS users, the plugin can be installed using the flake:

```nix
{ inputs, pkgs, ... }:
{
  services.pretix = {
    enable = true;
    plugins = [
      inputs.pretix-postfinance.packages.${pkgs.stdenv.hostPlatform.system}.default
    ];
  };
}
```

## Development

### Setup with uv

```bash
# Create virtual environment
uv venv

# Install with development dependencies
uv pip install -e ".[dev]"
```

### Setup with Nix

```bash
# Enter development shell
nix develop

# Or use direnv
direnv allow
```

### Running checks

```bash
# Run linting
uv run ruff check .

# Run type checking
uv run ty check pretix_postfinance/

# Run tests with coverage
uv run pytest tests/ --cov=pretix_postfinance --cov-report=term-missing -v
```

### Translations

Translatable strings live in `pretix_postfinance/locale/<lang>/LC_MESSAGES/`,
in two domains: `django.po` for Python and templates, `djangojs.po` for the
control panel JavaScript. The plugin ships French, German, Italian and
Spanish; the `.mo` files are compiled at build time and are not committed.

After changing any user-facing string, re-extract and fill in the new
messages:

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

Then translate the empty and `#, fuzzy` entries, drop the fuzzy markers, and
check the result compiles:

```bash
msgfmt --check --check-format --statistics -o /dev/null \
    pretix_postfinance/locale/*/LC_MESSAGES/*.po
```

Note that `xgettext` does not look inside f-strings, so a string only gets
extracted if `_()` wraps the whole literal — use `_("... {name} ...").format(...)`
rather than `f"... {_('...')} ..."`.

### Configuration

Configure the plugin in the event's payment settings:

- **Space ID**, **User ID**, **Authentication key**: the credentials of a
  PostFinance Checkout application user, created under Account > Users >
  Application Users. The authentication key is shown only once.
- **Test space ID**, **Test user ID**, **Test authentication key**: optional
  second set of credentials. When they are filled in, an event in test mode
  charges through the test space; without them a test mode event charges
  through the production space. There is no separate environment switch —
  pretix's own test mode is what selects between the two.
- **Display name** and **Description**: what customers see at checkout.
- **Allowed payment methods**: restricts which of the space's payment methods
  are offered. Save the credentials first, then reload the page to populate
  the list.

#### Sending callbacks to another host

"Setup webhooks" registers this instance's own address with PostFinance, which
is what you want in production. An instance PostFinance cannot reach under that
address — a staging instance behind a VPN, say — can name a different host in
`pretix.cfg`:

```ini
[postfinance]
webhook_base_url = https://pretix-staging.example.org
```

or through the equivalent `PRETIX_POSTFINANCE_WEBHOOK_BASE_URL` environment
variable. Only the scheme and host are taken from it; the path always comes
from the plugin's URL config, so the registered URL cannot drift away from the
route that actually serves it. The payment settings page shows the resulting
URL, so you can check it before pressing the button.

That host has to reach this instance over the public internet. Exposing only
`/_postfinance/webhook/` is enough — no other route needs to be public.

## Features

- Payment processing via PostFinance Checkout
- Full and partial refund support
- Webhook handling for payment and refund notifications
- Test mode support, including an opt-in option to offer the production space
  alongside the test space while in test mode, so it can be verified
  end-to-end before going live
- Optional alternative payment currency: customers can choose to be charged
  in a different currency (e.g. CHF on a EUR event) at a configured exchange
  rate (see below)
- Installment plans, where pretix supports them: the first installment is paid
  on the payment page and the rest are charged automatically against a stored
  payment method (see below)
- Translated into French, German, Italian and Spanish, for both the checkout
  pages customers see and the control panel organizers use

### Testing the production space

Switching on **Offer the production space in test mode** in the payment
settings makes checkout offer two PostFinance options while the event is in
test mode:

- **PostFinance (test space)** — uses the test credentials, no real money
- **PostFinance (production space)** — uses the live credentials

The option is **off by default** and requires test credentials. Because it
lives in the payment settings, only the people who can reach those settings
can expose the production space to buyers in test mode. Leave it off unless
you are actively running that verification, and switch it back off afterwards.

> [!WARNING]
> Payments through the production space option are **real charges**, even
> though the order is a test mode order. Refund or void them in your
> PostFinance dashboard before you disable test mode: pretix offers to delete
> all test mode orders at that point, and deleting the order does not undo the
> charge — it only removes your record of it.

Set up webhooks for both spaces (there is a separate "Setup webhooks" button
next to each set of credentials), otherwise payments in the space without a
webhook are never confirmed automatically.

### Charging in an alternative currency

Events priced in one currency (e.g. EUR) can offer customers the option to be
charged in another (e.g. CHF for Swiss customers). pretix itself keeps all
accounting (orders, invoices, refunds) in the event currency; only the charge
sent to PostFinance is converted.

To use it, set the **alternative payment currency** and the **exchange rate**
(how much of that currency is charged per 1 unit of the event currency) in
the provider settings. Include a small margin in the rate to cover exchange
rate fluctuations.

Customers then see a "Pay in CHF" checkbox with the converted amount when
they select PostFinance during checkout. The rate in effect when a payment is
started is stored on the payment, together with the amount actually charged.
Partial refunds are converted with that stored rate; a refund of the payment's
full remaining amount returns what is left of the stored charge instead, so
conversion rounding never leaves a cent behind or overshoots the transaction.
The charged amount and rate are shown to the customer during checkout and to
organizers in the order's payment details, and each refund shows the amount
PostFinance actually returned next to the transaction it was drawn on.

Cancelling an order with a cancellation fee works through the same path: pretix
sets the fee in the event currency and asks for a refund of the rest, which is
converted at the payment's stored rate. What the customer effectively keeps
paying is the charge minus that refund, so the fee is retained at the rate they
paid at — give or take a cent of rounding, since the refund is what gets
rounded, not the fee.

### Installment plans

On a pretix installation that supports installments, customers can split an
order into monthly installments and pay the first one during checkout. This
plugin implements the provider side of that:

- The first installment goes through the normal payment page, asking
  PostFinance to tokenize the payment method the customer used.
- The token is stored on the plan and every later installment is charged
  against it without the customer present, on the schedule pretix keeps.
- The token is deleted at PostFinance once the plan completes or is
  cancelled, so a stored payment method cannot be charged afterwards.
- Each automatic charge records its transaction on the payment, so an
  individual installment can be refunded through pretix like any other
  payment. A declined charge is recorded too, carrying PostFinance's reason.

Installments are only offered when the pretix installation provides them
(upstream pretix does not) and the event has them switched on. Enabling them,
the number of installments, the minimum order value and the grace period for a
failed charge are all pretix event settings, not plugin settings.

Because the token is what makes the later charges possible, the plugin stores
it from whichever path settles the first payment — the customer returning from
the payment page, or the transaction webhook. Set up webhooks: a customer who
closes the tab after paying otherwise leaves the plan without a token, and the
remaining installments cannot be charged.

Installments combine with the alternative payment currency. A plan bought in
the alternative currency is charged in that currency for its whole life: each
installment converts its own share at the rate that was quoted when the order
was placed, which is stored with the token. Changing the configured rate
afterwards does not reprice open plans. pretix keeps recording the payments in
the event currency, as it does for one-off payments.

## License

GNU Affero General Public License v3.0 (AGPLv3)
