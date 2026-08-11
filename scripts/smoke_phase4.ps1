# Phase 4 smoke test: concurrency.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# Concurrency here comes from Start-Job, so every request is issued by a separate
# PowerShell process and a separate curl.exe -- genuinely parallel at the OS level,
# with no shared event loop to serialise anything.
#
# Two honest caveats about this script versus the pytest harness:
#   * Start-Job costs a few hundred milliseconds per job to spin up, so requests
#     are staggered rather than simultaneous. The charge burst compensates by
#     holding each request in the processor for -LatencyMs, which is long enough
#     that the staggered starts still overlap.
#   * tests/test_concurrency.py is the rigorous proof (50 requests inside one
#     event loop, each on its own pooled connection). This script is the
#     end-to-end, real-sockets confirmation that it also holds over HTTP.
#
# Bodies go to temp files and are passed with --data-binary "@file" for the same
# reason as the earlier smoke scripts: nested quotes inside a curl.exe argument
# are mangled differently by PowerShell 5.1 and 7.

param(
    [string]$BaseUrl = "http://localhost:8000",
    [int]$Charges = 15,
    [int]$Withdrawals = 20,
    [int]$LatencyMs = 2000
)

$ErrorActionPreference = "Stop"

$Base = $BaseUrl.TrimEnd("/")
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
    $json = if ($lines.Count -gt 1) { ($lines[0..($lines.Count - 2)] -join "") } else { "" }

    [PSCustomObject]@{
        Status = $status
        Body   = if ($json) { $json | ConvertFrom-Json } else { $null }
        Raw    = $json
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

function Assert-AtLeast {
    param($Floor, $Actual, [string]$What)
    if ($Actual -ge $Floor) {
        Write-Host "  PASS  $What = $Actual (>= $Floor)" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- expected >= $Floor, got $Actual" -ForegroundColor Red
        exit 1
    }
}

# Fires $Count identical POSTs in parallel processes; returns the status codes.
function Invoke-Parallel {
    param(
        [string]$Url,
        [string]$BodyFile,
        [string]$IdempotencyKey,
        [int]$Count
    )

    $jobs = 1..$Count | ForEach-Object {
        Start-Job -ScriptBlock {
            param($url, $bodyFile, $key)
            $curlArgs = @(
                "-s", "-o", "NUL", "-w", "%{http_code}", "-X", "POST", $url,
                "-H", "Content-Type: application/json",
                "--data-binary", "@$bodyFile"
            )
            if ($key) { $curlArgs += @("-H", "Idempotency-Key: $key") }
            curl.exe @curlArgs
        } -ArgumentList $Url, $BodyFile, $IdempotencyKey
    }

    $codes = $jobs | Wait-Job | Receive-Job
    $jobs | Remove-Job
    return @($codes)
}

function Show-Codes {
    param([string[]]$Codes)
    $Codes | Group-Object | Sort-Object Name | ForEach-Object {
        Write-Host "        HTTP $($_.Name) x $($_.Count)"
    }
}

Write-Host "`n=== 1. Race 1: $Charges concurrent charges, ONE idempotency key ===" -ForegroundColor Cyan

$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 4 customer","currency":"INR"}'
Assert-Equal -Expected 201 -Actual $account.Status -What "POST /accounts status"
$accountId = $account.Body.id
Write-Host "  account = $accountId"

$key = [guid]::NewGuid().ToString()
Write-Host "  idempotency key = $key"
Write-Host "  each request holds the processor for ${LatencyMs}ms so the bursts overlap"

$chargeBody = @"
{
  "account_id": "$accountId",
  "amount": 250000,
  "force_latency_ms": $LatencyMs
}
"@
$chargeFile = Write-BodyFile -Name "phase4_charge.json" -Content $chargeBody

$chargeCodes = Invoke-Parallel -Url "$Base/charges" -BodyFile $chargeFile -IdempotencyKey $key -Count $Charges
Write-Host "  responses:"
Show-Codes -Codes $chargeCodes

$created = @($chargeCodes | Where-Object { $_ -eq "201" }).Count
Assert-AtLeast -Floor 1 -Actual $created -What "at least one request was served"

Write-Host "`n=== 2. Exactly ONE charge actually happened ===" -ForegroundColor Cyan

$balance = Invoke-Api -Method GET -Path "/accounts/$accountId/balance"
Assert-Equal -Expected 200 -Actual $balance.Status -What "GET balance status"
Assert-Equal -Expected 250000 -Actual $balance.Body.balance -What "balance moved exactly once"

Write-Host "`n=== 3. Race 2: fund 100000, then $Withdrawals concurrent withdrawals of 10000 ===" -ForegroundColor Cyan

$victim = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 4 withdrawer","currency":"INR"}'
$victimId = $victim.Body.id
Write-Host "  account = $victimId"

$fund = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$victimId`", `"amount`": 100000}" `
    -IdempotencyKey ([guid]::NewGuid().ToString())
Assert-Equal -Expected 201 -Actual $fund.Status -What "funding charge status"

$funded = Invoke-Api -Method GET -Path "/accounts/$victimId/balance"
Assert-Equal -Expected 100000 -Actual $funded.Body.balance -What "starting balance"

$withdrawBody = @"
{
  "account_id": "$victimId",
  "amount": 10000
}
"@
$withdrawFile = Write-BodyFile -Name "phase4_withdraw.json" -Content $withdrawBody

Write-Host "  demanding $($Withdrawals * 10000) from an account holding 100000..."
$withdrawCodes = Invoke-Parallel -Url "$Base/withdrawals" -BodyFile $withdrawFile -Count $Withdrawals
Write-Host "  responses:"
Show-Codes -Codes $withdrawCodes

$paid = @($withdrawCodes | Where-Object { $_ -eq "201" }).Count
Assert-Equal -Expected 10 -Actual $paid -What "withdrawals honoured (only 10 were affordable)"

Write-Host "`n=== 4. The account was never overdrawn ===" -ForegroundColor Cyan

$final = Invoke-Api -Method GET -Path "/accounts/$victimId/balance"
Assert-Equal -Expected 200 -Actual $final.Status -What "GET balance status"
Assert-Equal -Expected 0 -Actual $final.Body.balance -What "final balance"
Assert-AtLeast -Floor 0 -Actual $final.Body.balance -What "balance never went below the floor"

Write-Host "`nPhase 4 smoke: all checks passed.`n" -ForegroundColor Green
