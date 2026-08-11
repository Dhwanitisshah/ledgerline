# Phase 3 smoke test: idempotent charges.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# Same shape as smoke.ps1 / smoke_phase2.ps1: bodies go to temp files and are
# passed with --data-binary "@file", because nested double quotes inside a
# curl.exe argument are mangled differently by Windows PowerShell 5.1 and
# PowerShell 7. Files sidestep the whole problem.
#
# Requests here are strictly SERIAL. Two simultaneous requests on one key are a
# different problem and belong to Phase 4.

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
        [string]$Body,
        [string]$IdempotencyKey
    )

    $curlArgs = @("-s", "-w", "`n%{http_code}", "-X", $Method, "$Base$Path")
    if ($IdempotencyKey) {
        $curlArgs += @("-H", "Idempotency-Key: $IdempotencyKey")
    }
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

$key = [guid]::NewGuid().ToString()

Write-Host "`n=== 1. Create a customer account ===" -ForegroundColor Cyan

$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 3 customer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
$accountId = $account.Body.id
Write-Host "  account = $accountId"
Write-Host "  idempotency key = $key"

$chargeBody = @"
{
  "account_id": "$accountId",
  "amount": 250000
}
"@

Write-Host "`n=== 2. Charge 250000 paise with an Idempotency-Key ===" -ForegroundColor Cyan

$first = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $first.Status -What "first POST /charges status"
Assert-Equal -Expected "succeeded" -Actual $first.Body.status -What "payment status"
$paymentId = $first.Body.id
Write-Host "  payment = $paymentId"

$afterFirst = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 250000 -Actual $afterFirst.Body.balance -What "balance after the first charge"

Write-Host "`n=== 3. Send the IDENTICAL request again with the SAME key ===" -ForegroundColor Cyan

$second = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $second.Status -What "replayed POST /charges status"
Assert-Equal -Expected $paymentId -Actual $second.Body.id -What "same payment id (replayed, not recharged)"
Assert-Equal -Expected $first.Raw -Actual $second.Raw -What "response body is byte-identical"

Write-Host "`n=== 4. The balance moved ONCE, not twice ===" -ForegroundColor Cyan

$afterSecond = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 200 -Actual $afterSecond.Status -What "GET balance status"
Assert-Equal -Expected 250000 -Actual $afterSecond.Body.balance -What "balance UNCHANGED by the retry"

Write-Host "`n=== 5. Reuse the key with a DIFFERENT amount -- expect 4xx ===" -ForegroundColor Cyan

$mutatedBody = @"
{
  "account_id": "$accountId",
  "amount": 99999
}
"@

$mutated = Invoke-Api -Method POST -Path "/charges" -Body $mutatedBody -IdempotencyKey $key
Assert-Equal -Expected 422 -Actual $mutated.Status -What "reused key + different payload status"
Write-Host "  detail: $($mutated.Body.detail)"

$afterMutated = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 250000 -Actual $afterMutated.Body.balance -What "balance still unchanged"

Write-Host "`n=== 6. A missing Idempotency-Key is a 400 ===" -ForegroundColor Cyan

$noKey = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody
Assert-Equal -Expected 400 -Actual $noKey.Status -What "POST /charges without a key"
Write-Host "  detail: $($noKey.Body.detail)"

Write-Host "`n=== 7. A NEW key is a NEW charge ===" -ForegroundColor Cyan

$freshKey = [guid]::NewGuid().ToString()
$third = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody -IdempotencyKey $freshKey
Assert-Equal -Expected 201 -Actual $third.Status -What "new key POST /charges status"

if ($third.Body.id -ne $paymentId) {
    Write-Host "  PASS  new key produced a different payment = $($third.Body.id)" -ForegroundColor Green
}
else {
    Write-Host "  FAIL  new key replayed the old payment instead of charging" -ForegroundColor Red
    exit 1
}

$final = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 500000 -Actual $final.Body.balance -What "final balance (two real charges, one replay)"

Write-Host "`n=== 8. The failure path replays too ===" -ForegroundColor Cyan

$failKey = [guid]::NewGuid().ToString()
$failBody = @"
{
  "account_id": "$accountId",
  "amount": 4242,
  "force_outcome": "failure"
}
"@

$fail1 = Invoke-Api -Method POST -Path "/charges" -Body $failBody -IdempotencyKey $failKey
Assert-Equal -Expected 201 -Actual $fail1.Status -What "forced-failure charge status"
Assert-Equal -Expected "failed" -Actual $fail1.Body.status -What "payment status"

$fail2 = Invoke-Api -Method POST -Path "/charges" -Body $failBody -IdempotencyKey $failKey
Assert-Equal -Expected "failed" -Actual $fail2.Body.status -What "replayed status"
Assert-Equal -Expected $fail1.Raw -Actual $fail2.Raw -What "declined response is byte-identical on replay"

$reallyFinal = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 500000 -Actual $reallyFinal.Body.balance -What "declines still moved no money"

Write-Host "`nPhase 3 smoke: all checks passed.`n" -ForegroundColor Green
