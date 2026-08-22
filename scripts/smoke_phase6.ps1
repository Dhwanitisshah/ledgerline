# Phase 6 smoke test: refunds as reversing postings, and the invariant that bounds them.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# The story this script tells, end to end over real HTTP:
#
#   1. charge an account, then refund it in full -- the balance returns to zero and
#      the payment reaches 'refunded', a status that was unreachable for five phases
#   2. charge again, refund PART of it -- the balance moves by the partial amount and
#      the payment stays 'succeeded', because it is still partly live
#   3. try to refund more than is left -- 4xx, and nothing is written
#   4. double-submit one refund with one Idempotency-Key -- applied once
#   5. run the drift job, which compares our books against the processor's and
#      reports rather than repairs
#
# The balance assertions are the point of the whole phase. Nothing sets a balance
# back to zero: there is no balance column and nowhere to put one. The number falls
# because a reversing posting was added and the SUM changed, which is Phase 1's
# mechanism given a second posting to add up.
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

function New-ChargedAccount {
    param([string]$Name, [int]$ChargeAmount)

    $account = Invoke-Api -Method POST -Path "/accounts" -Body "{`"name`":`"$Name`",`"currency`":`"INR`"}"
    Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
    $accountId = $account.Body.id

    $chargeResponse = Invoke-Api -Method POST -Path "/charges" `
        -Body "{`"account_id`": `"$accountId`", `"amount`": $ChargeAmount}" `
        -IdempotencyKey ([guid]::NewGuid().ToString())
    Assert-Equal -Expected 201 -Actual $chargeResponse.Status -What "POST /charges status"
    Assert-Equal -Expected "succeeded" -Actual $chargeResponse.Body.status -What "charge status"

    [PSCustomObject]@{ AccountId = $accountId; PaymentId = $chargeResponse.Body.id }
}

function Get-Balance {
    param([string]$AccountId)
    $response = Invoke-Api -Method GET -Path "/accounts/$AccountId/balance"
    return $response.Body.balance
}

Write-Host "`n=== 1. A full refund reverses the charge completely ===" -ForegroundColor Cyan

$full = New-ChargedAccount -Name "Phase 6 full refund" -ChargeAmount $Amount
Write-Host "  account = $($full.AccountId)"
Write-Host "  payment = $($full.PaymentId)"
Assert-Equal -Expected $Amount -Actual (Get-Balance $full.AccountId) -What "balance after the charge"

# No amount in the body means "refund whatever is still refundable", which for an
# untouched charge is all of it.
$refund = Invoke-Api -Method POST -Path "/charges/$($full.PaymentId)/refund" `
    -Body "{}" -IdempotencyKey ([guid]::NewGuid().ToString())

Assert-Equal -Expected 201 -Actual $refund.Status -What "POST refund status"
Assert-Equal -Expected "succeeded" -Actual $refund.Body.status -What "refund status"
Assert-Equal -Expected $Amount -Actual $refund.Body.amount -What "refunded amount"
Assert-Equal -Expected 0 -Actual $refund.Body.remaining_refundable -What "remaining refundable"
Assert-Equal -Expected "refunded" -Actual $refund.Body.payment_status -What "payment status"
Assert-NotNull -Actual $refund.Body.ledger_transaction_id -What "reversing posting"

# The number below is not set by anything. It is a SUM over ledger_entries that now
# includes a debit of the same size as the original credit.
Assert-Equal -Expected 0 -Actual (Get-Balance $full.AccountId) -What "balance back to pre-charge"

$storedCharge = Invoke-Api -Method GET -Path "/charges/$($full.PaymentId)"
Assert-Equal -Expected "refunded" -Actual $storedCharge.Body.status -What "payment as stored"
# The charge's own posting is untouched -- a refund reverses, it does not edit.
Assert-NotNull -Actual $storedCharge.Body.ledger_transaction_id -What "charge keeps its posting"

Write-Host "`n=== 2. A partial refund leaves the payment succeeded ===" -ForegroundColor Cyan

$partial = New-ChargedAccount -Name "Phase 6 partial refund" -ChargeAmount $Amount
$part = 100000
Write-Host "  account = $($partial.AccountId)"

$firstPartial = Invoke-Api -Method POST -Path "/charges/$($partial.PaymentId)/refund" `
    -Body "{`"amount`": $part}" -IdempotencyKey ([guid]::NewGuid().ToString())

Assert-Equal -Expected 201 -Actual $firstPartial.Status -What "partial refund status"
Assert-Equal -Expected $part -Actual $firstPartial.Body.amount -What "partial amount"
Assert-Equal -Expected $part -Actual $firstPartial.Body.total_refunded -What "total refunded"
Assert-Equal -Expected ($Amount - $part) -Actual $firstPartial.Body.remaining_refundable `
    -What "remaining refundable"
# Still partly live, so still 'succeeded'. There is no 'partially_refunded' status:
# how much has come back is an amount, and amounts do not belong in a state machine.
Assert-Equal -Expected "succeeded" -Actual $firstPartial.Body.payment_status `
    -What "payment stays succeeded"
Assert-Equal -Expected ($Amount - $part) -Actual (Get-Balance $partial.AccountId) `
    -What "balance moved by the partial amount"

Write-Host "`n=== 3. Refunding more than is left is refused ===" -ForegroundColor Cyan

$tooMuch = $Amount  # only $Amount - $part is still refundable
$over = Invoke-Api -Method POST -Path "/charges/$($partial.PaymentId)/refund" `
    -Body "{`"amount`": $tooMuch}" -IdempotencyKey ([guid]::NewGuid().ToString())

Assert-Equal -Expected 422 -Actual $over.Status -What "over-refund is refused"
Write-Host "  -> $($over.Body.detail)"

# Nothing was written: the balance and the payment are exactly as section 2 left them.
Assert-Equal -Expected ($Amount - $part) -Actual (Get-Balance $partial.AccountId) `
    -What "balance unchanged by the rejected refund"

$refundList = Invoke-Api -Method GET -Path "/charges/$($partial.PaymentId)/refunds"
Assert-Equal -Expected 1 -Actual @($refundList.Body).Count -What "refund rows written"

Write-Host "  (the same over-refund is also unstorable from psql -- migration 0007" -ForegroundColor DarkGray
Write-Host "   installs a trigger that takes the payment's row lock and re-checks" -ForegroundColor DarkGray
Write-Host "   the sum, so bypassing the API does not bypass the invariant)" -ForegroundColor DarkGray

Write-Host "`n=== 4. One key, submitted twice, refunds once ===" -ForegroundColor Cyan

$idem = New-ChargedAccount -Name "Phase 6 idempotent refund" -ChargeAmount $Amount
$refundKey = [guid]::NewGuid().ToString()
Write-Host "  account = $($idem.AccountId)"
Write-Host "  idempotency key = $refundKey"

$body = "{`"amount`": $part}"
$firstSubmit = Invoke-Api -Method POST -Path "/charges/$($idem.PaymentId)/refund" `
    -Body $body -IdempotencyKey $refundKey
$secondSubmit = Invoke-Api -Method POST -Path "/charges/$($idem.PaymentId)/refund" `
    -Body $body -IdempotencyKey $refundKey

Assert-Equal -Expected 201 -Actual $firstSubmit.Status -What "first submit"
Assert-Equal -Expected 201 -Actual $secondSubmit.Status -What "second submit (replayed)"
Assert-Equal -Expected $firstSubmit.Raw -Actual $secondSubmit.Raw -What "byte-identical replay"

# Applied once: one refund row, and the balance moved by one partial amount.
$idemList = Invoke-Api -Method GET -Path "/charges/$($idem.PaymentId)/refunds"
Assert-Equal -Expected 1 -Actual @($idemList.Body).Count -What "refund rows (applied ONCE)"
Assert-Equal -Expected ($Amount - $part) -Actual (Get-Balance $idem.AccountId) `
    -What "balance moved exactly once"

# A different amount on the same key is a different refund, and is refused.
$conflicting = Invoke-Api -Method POST -Path "/charges/$($idem.PaymentId)/refund" `
    -Body "{`"amount`": 1}" -IdempotencyKey $refundKey
Assert-Equal -Expected 422 -Actual $conflicting.Status -What "same key, different amount"

Write-Host "`n=== 5. The drift job compares both sides and reports ===" -ForegroundColor Cyan

# --grace-seconds 0 examines everything settled, including the payments this script
# just created. Safe against an idle system, and the only way to see this run say
# anything about work from thirty seconds ago.
Push-Location $RepoRoot
try {
    $raw = (& $Python -m app.drift --json --grace-seconds 0 2>&1) -join ""
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAIL  drift job exited with $LASTEXITCODE" -ForegroundColor Red
        Write-Host "        $raw" -ForegroundColor Red
        exit 1
    }
}
finally {
    Pop-Location
}

$drift = $raw | ConvertFrom-Json
Write-Host "  examined $($drift.examined) settled payments"
Assert-Equal -Expected 0 -Actual @($drift.findings).Count -What "drift findings (books agree)"

if ($drift.examined -lt 3) {
    Write-Host "  FAIL  expected at least the 3 payments this script settled" -ForegroundColor Red
    exit 1
}
Write-Host "  PASS  the job examined this run's payments and found no disagreement" -ForegroundColor Green

Write-Host "`nPhase 6 smoke: all checks passed.`n" -ForegroundColor Green
Write-Host "Note what never happened above: no ledger row was updated or deleted." -ForegroundColor DarkGray
Write-Host "A refund is a NEW posting with the charge's legs reversed, so the balance" -ForegroundColor DarkGray
Write-Host "falls because the SUM changed -- not because anything set it. Corrections" -ForegroundColor DarkGray
Write-Host "are made by posting, which is what Phase 1 promised and Phase 6 spends." -ForegroundColor DarkGray
Write-Host ""
Write-Host "The drift job reports and does not repair. To see it find something," -ForegroundColor DarkGray
Write-Host "delete a row from the processor's books and run it again:" -ForegroundColor DarkGray
Write-Host '  docker compose exec postgres psql -U postgres -d ledgerline -c "DELETE FROM processor_refunds;"' -ForegroundColor DarkGray
Write-Host "  python -m app.drift --once --grace-seconds 0" -ForegroundColor DarkGray
Write-Host ""
