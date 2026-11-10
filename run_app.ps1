# Run the PulseBrief demo on Windows.
# If a project virtual environment exists, use it; otherwise fall back to `python`.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if (Test-Path ".venv\Scripts\python.exe") {
    ".venv\Scripts\python.exe"
} else {
    "python"
}

Write-Host "Starting PulseBrief with $python ..."
& $python -m streamlit run app.py @args
