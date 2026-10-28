{
  lib,
  fetchFromGitHub,
  buildPythonPackage,
  pythonOlder,
  setuptools,
  urllib3,
  pydantic,
  pyjwt,
  python-dateutil,
  typing-extensions,
  cryptography,
  flake8,
  mypy,
  pytest,
  tox,
  types-python-dateutil,
}:

buildPythonPackage rec {
  pname = "postfinancecheckout";
  version = "7.0.0";

  pyproject = true;

  src = fetchFromGitHub {
    owner = "pfpayments";
    repo = "python-sdk";
    rev = version;
    hash = "sha256-2vMk7ZA2cToBUzSTj5hMHO84pr912Imp0hWe3itfX+A=";
  };

  disabled = pythonOlder "3.11";

  build-system = [
    setuptools
  ];

  dependencies = [
    urllib3
    pydantic
    pyjwt
    python-dateutil
    typing-extensions
    cryptography
  ];

  pythonImportsCheck = [ "postfinancecheckout" ];

  passthru.optional-dependencies = {
    dev = [
      flake8
      mypy
      pytest
      tox
      types-python-dateutil
    ];
  };

  doCheck = false;

  meta = with lib; {
    description = "SDK that allows you to access PostFinance Checkout API";
    homepage = "https://github.com/pfpayments/python-sdk";
    license = licenses.asl20;
  };
}
