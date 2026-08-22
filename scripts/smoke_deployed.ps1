# Deployed smoke test: the charge -> replay -> refund flow against a LIVE URL.
#
#   .\scripts\smoke_deployed.ps1 -BaseUrl https://ledgerline.fly.dev
#
# This is the script you run after `fly deploy` to find out whether the thing that
# is now serving traffic actually works. It is deliberately different from the
# phase smoke scripts in three ways:
#
#   1. It assumes NOTHING about the environment. No fake-processor knobs, no
#      force_outcome, no force_crash_after_processor -- those are refused in
#      production (Phase 7), so a deployed smoke that used them would fail on a
#      correctly-configured deployment and pass on a dangerously-configured one.
#      That is exactly backwards, so this script only uses the real API surface.
#
#   2. It leaves the database in a state it is happy to leave behind. A deployment
#      is not a test database: this creates one account, charges it, and refunds
#      the charge in full, so the net effect on the ledger is zero.
#
#   3. It checks the OPERATIONAL surface too -- readiness, metrics, the request id
#      header, and that the ledger imbalance gauge is zero -- because "the API
#      answered" and "the deployment is healthy" are different claims.
#
# Exit code 0 means the live deployment moved money correctly and put it back.

param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl,
    [int]$Amount = 250000
)

$ErrorActionPreference = "Stop"

$Base = $BaseUrl.TrimEnd("/")
$TmpDir = Join-Path $env:TEMP "ledgerline-smoke"
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Body,
        [string]$IdempotencyKey
    )

    $headerFile = Join-Path $TmpDir "deployed-headers.txt"
    # --max-time: a live deployment can be cold-starting, but it should not take a
    # minute to answer. A hung request must fail the smoke rather than hang it.
    $curlArgs = @(
        "-s", "-D", $headerFile, "-w", "`n%{http_code}",
        "--max-time", "60", "-X", $Method, "$Base$Path"
    )
    if ($IdempotencyKey) { $curlArgs += @("-H", "Idempotency-Key: $IdempotencyKey") }
    if ($Body) {
        $bodyFile = Join-Path $TmpDir "deployed-body.json"
        [System.IO.File]::WriteAllText($bodyFile, $Body, $Utf8NoBom)
        $curlArgs += @("-H", "Content-Type: application/json", "--data-binary", "@$bodyFile")
    }

    $lines = @(curl.exe @curlArgs)
    if ($lines.Count -eq 0) {
        Write-Host "  FAIL  no response from $Base$Path" -ForegroundColor Red
        Write-Host "        Is the URL right, and is the service awake? A scale-to-zero" -ForegroundColor DarkGray
        Write-Host "        deployment needs a moment on the first request." -ForegroundColor DarkGray
        exit 1
    }

    $status = [int]$lines[-1]
    $raw = if ($lines.Count -gt 1) { ($lines[0..($lines.Count - 2)] -join "") } else { "" }

    $parsed = $null
    if ($raw) { try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null } }

    $headers = @{}
    if (Test-Path $headerFile) {
        foreach ($line in (Get-Content $headerFile)) {
            if ($line -match "^([^:]+):\s*(.*)$") { $headers[$matches[1].ToLower()] = $matches[2].Trim() }
        }
    }

    [PSCustomObject]@{ Status = $status; Body = $parsed; Raw = $raw; Headers = $headers }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$What)
    if ($Expected -eq $Actual) {
        Write-Host "  PASS  $What = $Actual" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected $Expected, got $Actual" -ForegroundColor Red
        exit 1
    }
}

function Assert-NotNull {
    param($Actual, [string]$What)
    if ($null -ne $Actual -and "$Actual" -ne "") {
        Write-Host "  PASS  $What = $Actual" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected a value, got nothing" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`nLedgerline deployed smoke -> $Base" -ForegroundColor Cyan

Write-Host "`n=== 0. The deployment is up and can reach its database ===" -ForegroundColor Cyan

$health = Invoke-Api -Method GET -Path "/health"
Assert-Equal -Expected 200 -Actual $health.Status -What "GET /health"
Assert-NotNull -Actual $health.Body.env -What "environment reported"
Assert-NotNull -Actual $health.Headers["x-request-id"] -What "request id header"

# Readiness is the one that proves the managed database is actually attached --
# the single most common way a first deploy is broken while looking fine.
$ready = Invoke-Api -Method GET -Path "/ready"
Assert-Equal -Expected 200 -Actual $ready.Status -What "GET /ready"
Assert-Equal -Expected "ok" -Actual $ready.Body.database -What "database reachable from the deployment"

if ($health.Body.env -ne "production") {
    Write-Host "  NOTE  APP_ENV is '$($health.Body.env)', not 'production'." -ForegroundColor Yellow
    Write-Host "        A real deployment should set APP_ENV=production so the fake" -ForegroundColor DarkGray
    Write-Host "        processor's test knobs are refused." -ForegroundColor DarkGray
}

Write-Host "`n=== 1. Charge an account ===" -ForegroundColor Cyan

$account = Invoke-Api -Method POST -Path "/accounts" `
    -Body '{"name":"deployed-smoke","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts"
$accountId = $account.Body.id
Write-Host "  account = $accountId"

$key = [guid]::NewGuid().ToString()
$chargeBody = "{`"account_id`": `"$accountId`", `"amount`": $Amount}"

$charge = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $charge.Status -What "POST /charges"
Assert-Equal -Expected "succeeded" -Actual $charge.Body.status -What "charge status"
Assert-NotNull -Actual $charge.Body.ledger_transaction_id -What "posting written"
$paymentId = $charge.Body.id

$balance = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected $Amount -Actual $balance.Body.balance -What "balance after the charge"

Write-Host "`n=== 2. Replay: same key, same body, charged ONCE ===" -ForegroundColor Cyan

$replay = Invoke-Api -Method POST -Path "/charges" -Body $chargeBody -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $replay.Status -What "replayed POST /charges"
Assert-Equal -Expected $paymentId -Actual $replay.Body.id -What "same payment id (replayed, not recharged)"
Assert-Equal -Expected $charge.Raw -Actual $replay.Raw -What "byte-identical response"

$afterReplay = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected $Amount -Actual $afterReplay.Body.balance -What "balance UNCHANGED by the retry"

Write-Host "`n=== 3. Refund in full ===" -ForegroundColor Cyan

$refund = Invoke-Api -Method POST -Path "/charges/$paymentId/refund" `
    -Body "{}" -IdempotencyKey ([guid]::NewGuid().ToString())
Assert-Equal -Expected 201 -Actual $refund.Status -What "POST refund"
Assert-Equal -Expected "succeeded" -Actual $refund.Body.status -What "refund status"
Assert-Equal -Expected $Amount -Actual $refund.Body.amount -What "refunded amount"
Assert-Equal -Expected "refunded" -Actual $refund.Body.payment_status -What "payment status"
Assert-Equal -Expected 0 -Actual $refund.Body.remaining_refundable -What "nothing left refundable"

# Back to zero by arithmetic: a reversing posting was added and the SUM changed.
# Nothing set this value, because there is no balance column to set.
$final = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 0 -Actual $final.Body.balance -What "balance back to pre-charge"

Write-Host "`n=== 4. The deployment's own books still balance ===" -ForegroundColor Cyan

$metrics = Invoke-Api -Method GET -Path "/metrics"
Assert-Equal -Expected 200 -Actual $metrics.Status -What "GET /metrics"

# The whole double-entry invariant, over every entry in the LIVE database.
if ($metrics.Raw -like "*ledgerline_ledger_imbalance_minor_units 0*") {
    Write-Host "  PASS  ledger imbalance is 0 across the live database" -ForegroundColor Green
}
else {
    Write-Host "  FAIL  ledger imbalance is NOT zero -- money exists outside the" -ForegroundColor Red
    Write-Host "        double-entry flows. Investigate before anything else." -ForegroundColor Red
    exit 1
}

foreach ($metric in @("ledgerline_charges_total", "ledgerline_refunds_total")) {
    if ($metrics.Raw -like "*$metric*") {
        Write-Host "  PASS  $metric is exported" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $metric missing from /metrics" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`nDeployed smoke: all checks passed against $Base`n" -ForegroundColor Green
Write-Host "Money moved and was put back, so this run left the ledger where it" -ForegroundColor DarkGray
Write-Host "found it. The outbox now holds two unpublished events (one charge, one" -ForegroundColor DarkGray
Write-Host "refund) until the publisher worker drains them -- which is the check" -ForegroundColor DarkGray
Write-Host "worth doing next:" -ForegroundColor DarkGray
Write-Host "  curl.exe $Base/metrics | Select-String ledgerline_outbox_pending" -ForegroundColor DarkGray
Write-Host "It should fall back to 0 within OUTBOX_INTERVAL_SECONDS. If it climbs" -ForegroundColor DarkGray
Write-Host "and never falls, the publisher machine is not running.`n" -ForegroundColor DarkGray
