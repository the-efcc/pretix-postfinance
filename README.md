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

## Features

- Payment processing via PostFinance Checkout
- Full and partial refund support
- Webhook handling for payment and refund notifications

## License

GNU Affero General Public License v3.0 (AGPLv3)
