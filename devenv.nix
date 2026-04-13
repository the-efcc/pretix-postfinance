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
    echo "Available tools: uv, ruff, ty, pytest"
  '';

  scripts = {
    run-ruff.exec = "uv run ruff check --fix";
    run-ty.exec = "uv run ty check pretix_postfinance";
    run-tests.exec = "uv run pytest tests";
  };
}
