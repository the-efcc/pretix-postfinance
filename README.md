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

### Configuration

Configure the plugin in your pretix settings with:

- **Space ID**: Your PostFinance Checkout space ID
- **User ID**: API user ID
- **API Secret**: API authentication secret
- **Environment**: `production` or `sandbox`

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
organizers in the order's payment details.

## License

GNU Affero General Public License v3.0 (AGPLv3)
