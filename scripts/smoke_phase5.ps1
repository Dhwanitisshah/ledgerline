# Phase 5b smoke test: the transactional outbox, and webhooks that arrive twice.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# The story this script tells, end to end over real HTTP:
#
#   1. a charge succeeds -- and the outbox already holds an event, unpublished,
#      written by the same commit that moved the money
#   2. the worker drains it: pending -> published, and the consumer has it once
#   3. running the worker again changes nothing, because the event is not pending
#   4. a charge dies mid-flight, leaving a payment stranded in 'processing'
#   5. the processor's webhook settles it -- and the SAME webhook, sent again,
#      does nothing at all: one posting, one balance movement, one event
#
# Like the Phase 5a script this one shells out to Python, because the publisher and
# the reconciler are deliberately separate processes rather than endpoints. Both
# support a JSON-emitting flag (`--status`, `--list`) so this script can read their
# state without a psql session and without parsing prose that somebody will later
# improve the wording of.
#
# Bodies go to temp files and are passed with --data-binary "@file" for the same
# reason as every earlier smoke script: nested quotes inside a curl.exe argument are
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
    # is not JSON. Parsed defensively rather than assumed.
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

function Invoke-Python {
    param([string[]]$PythonArgs, [switch]$Quiet)
    Push-Location $RepoRoot
    try {
        $output = & $Python @PythonArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  FAIL  python $PythonArgs exited with $LASTEXITCODE" -ForegroundColor Red
            $output | ForEach-Object { Write-Host "        $_" -ForegroundColor Red }
            exit 1
        }
        if (-not $Quiet) {
            $output | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
        }
        return $output
    }
    finally {
        Pop-Location
    }
}

# Outbox and delivery counts, straight from the publisher's own --status flag. It
# prints JSON on stdout and configures no logging on that path, so there is nothing
# else on the stream to confuse ConvertFrom-Json.
function Get-OutboxStatus {
    $raw = (Invoke-Python -PythonArgs @("-m", "app.publisher", "--status") -Quiet) -join ""
    return $raw | ConvertFrom-Json
}

function Write-Status {
    param($Status, [string]$Label)
    Write-Host ("        {0}: pending={1} published={2} delivered={3}" -f `
            $Label, $Status.pending, $Status.published, $Status.delivered) -ForegroundColor DarkGray
}

Write-Host "`n=== 1. A charge commits its event with the money ===" -ForegroundColor Cyan

$before = Get-OutboxStatus
Write-Status -Status $before -Label "before"

$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 5b customer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
$accountId = $account.Body.id
Write-Host "  account = $accountId"

$key = [guid]::NewGuid().ToString()
$charge = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$accountId`", `"amount`": $Amount}" -IdempotencyKey $key
Assert-Equal -Expected 201 -Actual $charge.Status -What "POST /charges status"
Assert-Equal -Expected "succeeded" -Actual $charge.Body.status -What "charge status"
Assert-NotNull -Actual $charge.Body.ledger_transaction_id -What "posting written"

# The request published nothing and called no broker. It inserted a row, in the
# transaction that moved the money, and returned. The event is durable already.
$afterCharge = Get-OutboxStatus
Write-Status -Status $afterCharge -Label "after the charge"
Assert-Equal -Expected ($before.pending + 1) -Actual $afterCharge.pending -What "pending outbox events"
Assert-Equal -Expected $before.delivered -Actual $afterCharge.delivered `
    -What "deliveries (unchanged -- the request published nothing)"

Write-Host "`n=== 2. The worker drains the outbox ===" -ForegroundColor Cyan

# A pass drains the whole backlog, not just this script's event -- there may well be
# leftovers from an earlier run or from the test suite. So the assertion is that
# everything pending became a delivery, which is the claim worth making anyway and
# is the one that holds no matter what state the database was already in.
$expectedDeliveries = $afterCharge.delivered + $afterCharge.pending

Invoke-Python -PythonArgs @("-m", "app.publisher", "--once") | Out-Null

$afterPublish = Get-OutboxStatus
Write-Status -Status $afterPublish -Label "after publishing"
Assert-Equal -Expected 0 -Actual $afterPublish.pending -What "pending events after the pass"
Assert-Equal -Expected $expectedDeliveries -Actual $afterPublish.delivered -What "deliveries"

Write-Host "`n=== 3. Publishing again changes nothing ===" -ForegroundColor Cyan

Invoke-Python -PythonArgs @("-m", "app.publisher", "--once") | Out-Null

$afterSecondPass = Get-OutboxStatus
Write-Status -Status $afterSecondPass -Label "after a second pass"
Assert-Equal -Expected $afterPublish.delivered -Actual $afterSecondPass.delivered -What "deliveries (unchanged)"
Assert-Equal -Expected $afterPublish.published -Actual $afterSecondPass.published -What "published (unchanged)"

Write-Host "`n=== 4. A charge dies, stranding a payment in 'processing' ===" -ForegroundColor Cyan

$hookAccount = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 5b webhook","currency":"INR"}'
$hookAccountId = $hookAccount.Body.id
Write-Host "  account = $hookAccountId"

$crashBody = @"
{
  "account_id": "$hookAccountId",
  "amount": $Amount,
  "force_crash_after_processor": true
}
"@

$crashed = Invoke-Api -Method POST -Path "/charges" `
    -Body $crashBody -IdempotencyKey ([guid]::NewGuid().ToString())
Assert-Equal -Expected 500 -Actual $crashed.Status -What "the charge died mid-flight"

$balance = Invoke-Api -Method GET -Path "/accounts/$hookAccountId/balance"
Assert-Equal -Expected 0 -Actual $balance.Body.balance -What "balance (card charged, ledger empty)"

# `--list` answers "what would the sweep do?" without doing it. The array is ordered
# oldest first, so the payment just stranded is the last one.
$stuckRaw = (Invoke-Python -PythonArgs @(
        "-m", "app.reconcile", "--list", "--stuck-after-seconds", "0") -Quiet) -join ""
$stuck = @($stuckRaw | ConvertFrom-Json)
if ($stuck.Count -lt 1) {
    Write-Host "  FAIL  expected at least one stranded payment, got none" -ForegroundColor Red
    exit 1
}
$paymentId = $stuck[-1]
Write-Host "  stranded payment = $paymentId"

Write-Host "`n=== 5. The processor's webhook settles it ===" -ForegroundColor Cyan

$eventId = "evt_" + [guid]::NewGuid().ToString("N")
$hookBody = @"
{
  "id": "$eventId",
  "type": "charge.succeeded",
  "data": { "attempt_ref": "$paymentId" }
}
"@

$hook = Invoke-Api -Method POST -Path "/webhooks" -Body $hookBody
Assert-Equal -Expected 200 -Actual $hook.Status -What "POST /webhooks status"
Assert-Equal -Expected $false -Actual $hook.Body.duplicate -What "first delivery is not a duplicate"
Assert-Equal -Expected "settled_succeeded" -Actual $hook.Body.outcome -What "outcome"

$settled = Invoke-Api -Method GET -Path "/accounts/$hookAccountId/balance"
Assert-Equal -Expected $Amount -Actual $settled.Body.balance -What "balance after the webhook"

$afterHook = Get-OutboxStatus
Write-Status -Status $afterHook -Label "after the webhook"
# Money moved in the webhook's transaction, so an event was owed in that same
# transaction. The recovery path emits exactly what the charge route would have.
Assert-Equal -Expected ($afterSecondPass.pending + 1) -Actual $afterHook.pending `
    -What "the settlement's outbox event"

Write-Host "`n=== 6. The SAME webhook again does nothing ===" -ForegroundColor Cyan

# Byte-identical body, same event id -- exactly what a provider sends when it does
# not hear back in time. Providers deliver at least once; there is no opting out.
$replay = Invoke-Api -Method POST -Path "/webhooks" -Body $hookBody
Assert-Equal -Expected 200 -Actual $replay.Status -What "duplicate delivery still returns 200"
Assert-Equal -Expected $true -Actual $replay.Body.duplicate -What "second delivery is a duplicate"
Assert-Equal -Expected $hook.Body.outcome -Actual $replay.Body.outcome -What "outcome (unchanged)"

$unchanged = Invoke-Api -Method GET -Path "/accounts/$hookAccountId/balance"
Assert-Equal -Expected $Amount -Actual $unchanged.Body.balance -What "balance moved exactly ONCE"

$afterReplay = Get-OutboxStatus
Write-Status -Status $afterReplay -Label "after the duplicate"
Assert-Equal -Expected $afterHook.pending -Actual $afterReplay.pending -What "no second event emitted"

$recorded = Invoke-Api -Method GET -Path "/webhooks/$eventId"
Assert-Equal -Expected 200 -Actual $recorded.Status -What "one webhook record"
Assert-Equal -Expected "settled_succeeded" -Actual $recorded.Body.outcome -What "recorded outcome"

Write-Host "`n=== 7. And the settlement's event publishes like any other ===" -ForegroundColor Cyan

$expectedFinal = $afterReplay.delivered + $afterReplay.pending

Invoke-Python -PythonArgs @("-m", "app.publisher", "--once") | Out-Null

$final = Get-OutboxStatus
Write-Status -Status $final -Label "final"
Assert-Equal -Expected 0 -Actual $final.pending -What "nothing left pending"
Assert-Equal -Expected $expectedFinal -Actual $final.delivered -What "deliveries"

Write-Host "`nPhase 5b smoke: all checks passed.`n" -ForegroundColor Green
Write-Host "What was never done anywhere above: a broker call after a commit." -ForegroundColor DarkGray
Write-Host "The event was written by the transaction that moved the money, and a" -ForegroundColor DarkGray
Write-Host "separate worker delivered it afterwards. At-least-once delivery," -ForegroundColor DarkGray
Write-Host "exactly-once effect -- the duplicate in section 6 is that, observed.`n" -ForegroundColor DarkGray
