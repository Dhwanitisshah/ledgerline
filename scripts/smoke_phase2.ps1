# Phase 2 smoke test: the payment lifecycle and the atomicity guarantee.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# The shape here matches scripts\smoke.ps1: bodies go to temp files and are passed
# with --data-binary "@file" rather than inlined, because nested double quotes
# inside a curl.exe argument are mangled differently by Windows PowerShell 5.1 and
# PowerShell 7. Files sidestep the whole problem.
#
# The forced failure is driven by "force_outcome": "failure" in the request body,
# so this script never needs the server restarted under different env vars.

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

function Assert-Null {
    param(
        $Actual,
        [string]$What
    )

    if ($null -eq $Actual) {
        Write-Host "  PASS  $What is null" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected null, got '$Actual'" -ForegroundColor Red
        exit 1
    }
}

function Assert-NotNull {
    param(
        $Actual,
        [string]$What
    )

    if ($null -ne $Actual -and "$Actual" -ne "") {
        Write-Host "  PASS  $What = $Actual" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected a value, got null" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`n=== 1. Create a customer account ===" -ForegroundColor Cyan

$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 2 customer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
$accountId = $account.Body.id
Write-Host "  account = $accountId"

$opening = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 200 -Actual $opening.Status -What "GET balance status"
Assert-Equal -Expected 0 -Actual $opening.Body.balance -What "opening balance"

Write-Host "`n=== 2. Charge 250000 paise (INR 2500.00) -- expect success ===" -ForegroundColor Cyan

$chargeBody = @"
{
  "account_id": "$accountId",
  "amount": 250000
}
"@

$charge = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody
Assert-Equal -Expected 201 -Actual $charge.Status -What "POST /charges status"
Assert-Equal -Expected "succeeded" -Actual $charge.Body.status -What "payment status"
Assert-NotNull -Actual $charge.Body.processor_ref -What "processor_ref"
Assert-NotNull -Actual $charge.Body.ledger_transaction_id -What "ledger_transaction_id"
Assert-Null -Actual $charge.Body.failure_reason -What "failure_reason"
$succeededId = $charge.Body.id

Write-Host "`n=== 3. The balance moved ===" -ForegroundColor Cyan

$afterCharge = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 200 -Actual $afterCharge.Status -What "GET balance status"
Assert-Equal -Expected 250000 -Actual $afterCharge.Body.balance -What "balance after a successful charge"

Write-Host "`n=== 4. State persisted -- read the payment back ===" -ForegroundColor Cyan

$fetched = Invoke-Api -Method GET -Path "/charges/$succeededId"
Assert-Equal -Expected 200 -Actual $fetched.Status -What "GET /charges/{id} status"
Assert-Equal -Expected "succeeded" -Actual $fetched.Body.status -What "persisted status"
Assert-Equal -Expected $charge.Body.ledger_transaction_id -Actual $fetched.Body.ledger_transaction_id -What "persisted ledger_transaction_id"

Write-Host "`n=== 5. Force a processor failure -- THE point of this phase ===" -ForegroundColor Cyan

$balanceBefore = $afterCharge.Body.balance

$failBody = @"
{
  "account_id": "$accountId",
  "amount": 99999,
  "force_outcome": "failure"
}
"@

$failed = Invoke-Api -Method POST -Path "/charges" -Body $failBody
# 201, not 4xx: the payment resource was created and the outcome recorded. A
# decline is a business result, not a failed request. Read `status` for which.
Assert-Equal -Expected 201 -Actual $failed.Status -What "POST /charges (forced failure) status"
Assert-Equal -Expected "failed" -Actual $failed.Body.status -What "payment status"
Assert-NotNull -Actual $failed.Body.failure_reason -What "failure_reason"
Assert-Null -Actual $failed.Body.ledger_transaction_id -What "ledger_transaction_id (no money moved)"
$failedId = $failed.Body.id

Write-Host "`n=== 6. The ledger was not touched ===" -ForegroundColor Cyan

$afterFailure = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 200 -Actual $afterFailure.Status -What "GET balance status"
Assert-Equal -Expected $balanceBefore -Actual $afterFailure.Body.balance -What "balance UNCHANGED after a declined charge"

$failedFetched = Invoke-Api -Method GET -Path "/charges/$failedId"
Assert-Equal -Expected 200 -Actual $failedFetched.Status -What "GET /charges/{id} status"
Assert-Equal -Expected "failed" -Actual $failedFetched.Body.status -What "persisted status"
Assert-Null -Actual $failedFetched.Body.ledger_transaction_id -What "persisted ledger_transaction_id"

Write-Host "`n=== 7. Injected latency, still atomic ===" -ForegroundColor Cyan

$slowBody = @"
{
  "account_id": "$accountId",
  "amount": 500,
  "force_latency_ms": 250
}
"@

$slow = Invoke-Api -Method POST -Path "/charges" -Body $slowBody
Assert-Equal -Expected 201 -Actual $slow.Status -What "POST /charges (250ms latency) status"
Assert-Equal -Expected "succeeded" -Actual $slow.Body.status -What "payment status"

$final = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 250500 -Actual $final.Body.balance -What "final balance (250000 + 500, the decline contributed nothing)"

Write-Host "`n=== 8. Reject a charge against an unknown account ===" -ForegroundColor Cyan

$ghostBody = @"
{
  "account_id": "00000000-0000-0000-0000-000000000000",
  "amount": 100
}
"@

$ghost = Invoke-Api -Method POST -Path "/charges" -Body $ghostBody
Assert-Equal -Expected 404 -Actual $ghost.Status -What "POST /charges (unknown account) status"

Write-Host "`nPhase 2 smoke: all checks passed.`n" -ForegroundColor Green
