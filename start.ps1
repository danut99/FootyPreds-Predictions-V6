$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Nu s-a putut crea mediul Python.' }
    & $pythonExe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Instalarea dependențelor a eșuat.' }
}
Write-Host 'FootyPreds V7: http://127.0.0.1:8000 (Ctrl+C pentru oprire)'
& $pythonExe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
