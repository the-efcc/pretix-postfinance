{
  languages.python = {
    enable = true;
    version = "3.11";

    uv = {
      enable = true;
      sync = {
        enable = true;
        allExtras = true;
      };
    };
  };

  enterShell = ''
    echo "Python development environment loaded"
    echo "Available tools: uv, ruff, mypy, pytest"
  '';

  scripts = {
    run-ruff.exec = "uv run ruff check --fix";
    run-mypy.exec = "uv run mypy pretix_postfinance";
    run-tests.exec = "uv run pytest tests";
  };
}
