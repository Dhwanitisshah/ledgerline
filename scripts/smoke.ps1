# Phase 1 smoke test. Requires the app running on $BaseUrl
# (see the README for the ordered startup commands).
#
# Bodies are written to temp files and passed with --data-binary "@file" rather
# than inlined, because nested double quotes inside a curl.exe argument get
# mangled differently by Windows PowerShell 5.1 and PowerShell 7. Files sidestep
# the whole problem.

param(
    [string]$BaseUrl = "http://localhost:8000"
)

$ErrorActionPreference = "Stop"

$Base = $BaseUrl.TrimEnd("/")
$TmpDir = Join-Path $env:TEMP "ledgerline-smoke"
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

# Deliberately not Set-Content -Encoding utf8: on Windows PowerShell 5.1 that
# emits a BOM, and a BOM at the front of a request body is not valid JSON.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Body
    )

    $curlArgs = @("-s", "-w", "`n%{http_code}", "-X", $Method, "$Base$Path")
    if ($Body) {
        $bodyFile = Join-Path $TmpDir "body.json"
        [System.IO.File]::WriteAllText($bodyFile, $Body, $Utf8NoBom)
        $curlArgs += @("-H", "Content-Type: application/json", "--data-binary", "@$bodyFile")
    }

    $lines = @(curl.exe @curlArgs)
    if ($lines.Count -eq 0) {
        Write-Host "  FAIL  no response from $Base$Path -- is uvicorn running?" -ForegroundColor Red
        exit 1
    }

    $status = [int]$lines[-1]
    $json = if ($lines.Count -gt 1) { ($lines[0..($lines.Count - 2)] -join "") } else { "" }

    [PSCustomObject]@{
        Status = $status
        Body   = if ($json) { $json | ConvertFrom-Json } else { $null }
        Raw    = $json
    }
}

function Assert-Equal {
    param(
        $Expected,
        $Actual,
        [string]$What
    )

    if ($Expected -eq $Actual) {
        Write-Host "  PASS  $What = $Actual" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected $Expected, got $Actual" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`n=== 1. Create two accounts ===" -ForegroundColor Cyan

$payer = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Payer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $payer.Status -What "POST /accounts (payer) status"
$payerId = $payer.Body.id
Write-Host "  payer  = $payerId"

$payee = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Payee","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $payee.Status -What "POST /accounts (payee) status"
$payeeId = $payee.Body.id
Write-Host "  payee  = $payeeId"

Write-Host "`n=== 2. Post a balanced transfer of 250000 paise (INR 2500.00) ===" -ForegroundColor Cyan

$transferBody = @"
{
  "description": "smoke: payer -> payee",
  "entries": [
    {"account_id": "$payerId", "direction": "debit",  "amount": 250000},
    {"account_id": "$payeeId", "direction": "credit", "amount": 250000}
  ]
}
"@

$transfer = Invoke-Api -Method POST -Path "/transactions" -Body $transferBody
Assert-Equal -Expected 201 -Actual $transfer.Status -What "POST /transactions (balanced) status"
Write-Host "  transaction = $($transfer.Body.id)"

Write-Host "`n=== 3. Check both derived balances ===" -ForegroundColor Cyan

$payerBalance = Invoke-Api -Method GET -Path "/accounts/$payerId/balance"
Assert-Equal -Expected 200 -Actual $payerBalance.Status -What "GET payer balance status"
Assert-Equal -Expected (-250000) -Actual $payerBalance.Body.balance -What "payer balance (debited)"

$payeeBalance = Invoke-Api -Method GET -Path "/accounts/$payeeId/balance"
Assert-Equal -Expected 200 -Actual $payeeBalance.Status -What "GET payee balance status"
Assert-Equal -Expected 250000 -Actual $payeeBalance.Body.balance -What "payee balance (credited)"

$total = $payerBalance.Body.balance + $payeeBalance.Body.balance
Assert-Equal -Expected 0 -Actual $total -What "the two balances sum to zero"

Write-Host "`n=== 4. Fire an unbalanced posting -- expect 422 ===" -ForegroundColor Cyan

$unbalancedBody = @"
{
  "description": "smoke: debits do not match credits",
  "entries": [
    {"account_id": "$payerId", "direction": "debit",  "amount": 250000},
    {"account_id": "$payeeId", "direction": "credit", "amount": 249999}
  ]
}
"@

$unbalanced = Invoke-Api -Method POST -Path "/transactions" -Body $unbalancedBody
Assert-Equal -Expected 422 -Actual $unbalanced.Status -What "POST /transactions (unbalanced) status"
Write-Host "  detail: $($unbalanced.Body.detail)"

Write-Host "`n=== 5. Confirm the rejected posting wrote nothing ===" -ForegroundColor Cyan

$payerAfter = Invoke-Api -Method GET -Path "/accounts/$payerId/balance"
$payeeAfter = Invoke-Api -Method GET -Path "/accounts/$payeeId/balance"
Assert-Equal -Expected (-250000) -Actual $payerAfter.Body.balance -What "payer balance unchanged after rollback"
Assert-Equal -Expected 250000 -Actual $payeeAfter.Body.balance -What "payee balance unchanged after rollback"

Write-Host "`n=== 6. Fire a mixed-currency posting -- expect 422 ===" -ForegroundColor Cyan

$usd = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Dollar account","currency":"USD"}'
Assert-Equal -Expected 201 -Actual $usd.Status -What "POST /accounts (USD) status"
$usdId = $usd.Body.id

# Equal integers, different units: 100 paise is not 100 cents.
$mixedBody = @"
{
  "description": "smoke: paise against cents",
  "entries": [
    {"account_id": "$payerId", "direction": "debit",  "amount": 100},
    {"account_id": "$usdId",   "direction": "credit", "amount": 100}
  ]
}
"@

$mixed = Invoke-Api -Method POST -Path "/transactions" -Body $mixedBody
Assert-Equal -Expected 422 -Actual $mixed.Status -What "POST /transactions (mixed currency) status"
Write-Host "  detail: $($mixed.Body.detail)"

$payerFinal = Invoke-Api -Method GET -Path "/accounts/$payerId/balance"
$usdFinal = Invoke-Api -Method GET -Path "/accounts/$usdId/balance"
Assert-Equal -Expected (-250000) -Actual $payerFinal.Body.balance -What "payer balance unchanged"
Assert-Equal -Expected 0 -Actual $usdFinal.Body.balance -What "USD account never touched"

Write-Host "`nPhase 1 smoke: all checks passed.`n" -ForegroundColor Green
