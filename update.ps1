$ErrorActionPreference = "Continue"

if (Test-Path .env) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}
$TOKEN = $env:GITHUB_TOKEN

# Config for add-submodule workflow trigger
$VERSION = "boost-1.90.0"
$SUBMODULES = @("json", "unordered")
$LANG_CODE = "zh_Hans"
$EXTENSIONS = @(".adoc")   # e.g. @(".adoc", ".md") or @() for all supported

function Invoke-Step($step, $desc, [scriptblock]$cmd) {
    Write-Host "[$step] $desc"
    & $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED at step $step (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

Invoke-Step "4/5" "Triggering add-submodule workflow..." {
    python .\trigger_add_submodule.py --token $TOKEN --version $VERSION --submodules $SUBMODULES --lang-code $LANG_CODE --extensions $EXTENSIONS
}

Write-Host "Done."
