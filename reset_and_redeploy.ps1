$ErrorActionPreference = "Continue"

$TOKEN = "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"

function Invoke-Step($step, $desc, [scriptblock]$cmd) {
    Write-Host "[$step] $desc"
    & $cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED at step $step (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

Invoke-Step "1/5" "Deleting CppDigest lib repos..." {
    python .\delete_cppdigest_lib_repos.py --token $TOKEN --submodules algorithm system --yes
}

Write-Host "[2/5] Removing local libs folder and .gitmodules..."
if (Test-Path libs) { Remove-Item -Recurse -Force libs }
if (Test-Path .gitmodules) { Remove-Item -Force .gitmodules }

Invoke-Step "3/5" "Pushing project..." {
    python .\push_project.py
}

Invoke-Step "4/5" "Triggering add-submodule workflow..." {
    python .\trigger_add_submodule.py --token $TOKEN --version boost-1.90.0 --submodules algorithm system --lang-code zh_Hans
}

Write-Host "[5/5] Waiting 10s for workflow to finish..."
Start-Sleep -Seconds 10

Invoke-Step "5/5" "Pulling (allow unrelated histories)..." {
    git pull origin master --allow-unrelated-histories
}

Write-Host "Done."
