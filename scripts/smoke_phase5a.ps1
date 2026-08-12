# Phase 5a smoke test: the crash between the processor and COMMIT, and its recovery.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# The story this script tells, end to end over real HTTP:
#
#   1. a charge dies at the fatal instant -- the card is charged, we commit nothing
#   2. the money is NOT in the ledger, and the customer's retry is locked out (409)
#   3. the reconciler asks the processor what happened and finishes the job
#   4. the retry now replays the reconciled outcome, and the balance moved ONCE
#
# Unlike the earlier smoke scripts this one shells out to Python, because the
# reconciler is deliberately a separate process rather than an endpoint. Recovery
# that runs inside the process being recovered from is unavailable exactly when it
# is needed. `python -m app.reconcile --once` is the same entry point the long-lived
# worker uses; --once makes it terminate instead of looping.
#
# Bodies go to temp files and are passed with --data-binary "@file" for the same
# reason as the earlier smoke scripts: nested quotes inside a curl.exe argument are
# mangled differently by PowerShell 5.1 and 7.

param(
    [string]$BaseUrl = "http://localhost:8000",
    [string]$Python = ".\.venv\Scripts\python.exe",
    [int]$Amount = 250000
)

$ErrorActionPreference = "Stop"

$Base = $BaseUrl.TrimEnd("/")
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TmpDir = Join-Path $env:TEMP "ledgerline-smoke"
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Write-BodyFile {
    param([string]$Name, [string]$Content)
    $path = Join-Path $TmpDir $Name
    [System.IO.File]::WriteAllText($path, $Content, $Utf8NoBom)
    return $path
}

function Invoke-Api {
    param(
        [string]$Method,
        [string]$Path,
        [string]$Body,
        [string]$IdempotencyKey
    )

    $curlArgs = @("-s", "-w", "`n%{http_code}", "-X", $Method, "$Base$Path")
    if ($IdempotencyKey) { $curlArgs += @("-H", "Idempotency-Key: $IdempotencyKey") }
    if ($Body) {
        $bodyFile = Write-BodyFile -Name "body.json" -Content $Body
        $curlArgs += @("-H", "Content-Type: application/json", "--data-binary", "@$bodyFile")
    }

    $lines = @(curl.exe @curlArgs)
    if ($lines.Count -eq 0) {
        Write-Host "  FAIL  no response from $Base$Path -- is uvicorn running?" -ForegroundColor Red
        exit 1
    }

    $status = [int]$lines[-1]
    $raw = if ($lines.Count -gt 1) { ($lines[0..($lines.Count - 2)] -join "") } else { "" }

    # A deliberately crashed request answers with Starlette's plain-text 500, which
    # is not JSON. Parsed defensively rather than assumed, because that response is
    # the point of the first section.
    $parsed = $null
    if ($raw) {
        try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null }
    }

    [PSCustomObject]@{
        Status = $status
        Body   = $parsed
        Raw    = $raw
    }
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

# Runs one reconciliation pass. --stuck-after-seconds 0 makes every payment
# currently in 'processing' a candidate, which is what you want against an idle
# system and is safe regardless: the row lock and the status re-check decide who
# settles a payment, not the threshold.
function Invoke-Sweep {
    Push-Location $RepoRoot
    try {
        $output = & $Python -m app.reconcile --once --stuck-after-seconds 0 2>&1
        $output | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  FAIL  reconciler exited with $LASTEXITCODE" -ForegroundColor Red
            exit 1
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host "`n=== 1. A charge that dies right after the processor says yes ===" -ForegroundColor Cyan

$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 5a customer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
$accountId = $account.Body.id
Write-Host "  account = $accountId"

$key = [guid]::NewGuid().ToString()
Write-Host "  idempotency key = $key"

$crashBody = @"
{
  "account_id": "$accountId",
  "amount": $Amount,
  "force_crash_after_processor": true
}
"@

$crashed = Invoke-Api -Method POST -Path "/charges" -Body $crashBody -IdempotencyKey $key
Assert-Equal -Expected 500 -Actual $crashed.Status -What "the charge died mid-flight"
Write-Host "  the card has been charged. Nothing was committed on our settlement side."

Write-Host "`n=== 2. The money is missing and the customer is locked out ===" -ForegroundColor Cyan

$balance = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 0 -Actual $balance.Body.balance -What "balance (card charged, ledger empty)"

# The retry proves a durable payment exists: 409 means a claim is held by a charge
# that is 'in_progress'. Under the old single-transaction flow this would be a 201
# and a SECOND charge on the same card, because nothing survived to say otherwise.
$blocked = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$accountId`", `"amount`": $Amount}" -IdempotencyKey $key
Assert-Equal -Expected 409 -Actual $blocked.Status -What "retry is refused while unresolved"
Write-Host "  -> $($blocked.Body.detail)"

Write-Host "`n=== 3. The reconciler asks the processor what actually happened ===" -ForegroundColor Cyan

Invoke-Sweep

Write-Host "`n=== 4. Settled: the posting is written and the retry gets its answer ===" -ForegroundColor Cyan

$replayed = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$accountId`", `"amount`": $Amount}" -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $replayed.Status -What "retry now replays the outcome"
Assert-Equal -Expected "succeeded" -Actual $replayed.Body.status -What "reconciled status"
Assert-NotNull -Actual $replayed.Body.ledger_transaction_id -What "posting written by the sweep"

$settled = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected $Amount -Actual $settled.Body.balance -What "balance moved exactly once"

Write-Host "`n=== 5. Sweeping again changes nothing ===" -ForegroundColor Cyan

Invoke-Sweep

$again = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected $Amount -Actual $again.Body.balance -What "balance after a second sweep"

Write-Host "`n=== 6. A crash after a DECLINE reconciles to failed, and moves nothing ===" -ForegroundColor Cyan

$declined = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 5a declined","currency":"INR"}'
$declinedId = $declined.Body.id
$declinedKey = [guid]::NewGuid().ToString()
Write-Host "  account = $declinedId"

$declineBody = @"
{
  "account_id": "$declinedId",
  "amount": $Amount,
  "force_outcome": "failure",
  "force_crash_after_processor": true
}
"@

$deadDecline = Invoke-Api -Method POST -Path "/charges" -Body $declineBody -IdempotencyKey $declinedKey
Assert-Equal -Expected 500 -Actual $deadDecline.Status -What "the declined charge died mid-flight"

Invoke-Sweep

# The sweep asked, and was told the card was refused. It did not assume a stuck
# payment must have succeeded -- which would have credited an account for a charge
# that never happened.
$declineReplay = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$declinedId`", `"amount`": $Amount}" -IdempotencyKey $declinedKey
Assert-Equal -Expected 201 -Actual $declineReplay.Status -What "retry replays the decline"
Assert-Equal -Expected "failed" -Actual $declineReplay.Body.status -What "reconciled status"
Assert-Equal -Expected $null -Actual $declineReplay.Body.ledger_transaction_id -What "no posting"

$declinedBalance = Invoke-Api -Method GET -Path "/accounts/$declinedId/balance"
Assert-Equal -Expected 0 -Actual $declinedBalance.Body.balance -What "balance after a reconciled decline"

Write-Host "`nPhase 5a smoke: all checks passed.`n" -ForegroundColor Green
Write-Host "To watch the gap itself, restart the server on the preserved flow:" -ForegroundColor DarkGray
Write-Host '  $env:CHARGE_DURABILITY = "single_txn"; uvicorn app.main:app' -ForegroundColor DarkGray
Write-Host "and run section 1 again -- the card is charged, and there is no payment" -ForegroundColor DarkGray
Write-Host "row at all for the sweep to find.`n" -ForegroundColor DarkGray
