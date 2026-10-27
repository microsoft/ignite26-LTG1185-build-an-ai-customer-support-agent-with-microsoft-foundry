# azd postdeploy hook (Windows): run scripts/setup.py in a local virtual environment.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $root ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating .venv for the setup script..."
    python -m venv $venv
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --quiet --disable-pip-version-check -r (Join-Path $PSScriptRoot "requirements.txt")
& $python (Join-Path $PSScriptRoot "setup.py")
exit $LASTEXITCODE
