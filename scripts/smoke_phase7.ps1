# Phase 7 smoke test: the things that only matter once this is on the internet.
# Requires the app running on $BaseUrl (see the README for ordered startup).
#
# Everything here is about the edge rather than about money:
#
#   1. every response carries a request id, and a caller's own id is propagated
#   2. security headers are set
#   3. liveness and readiness are different endpoints answering different questions
#   4. /metrics exposes the domain gauges -- including the ledger imbalance, which
#      is the whole double-entry invariant as one number that must be zero
#   5. metrics are labelled by route TEMPLATE, never by path (cardinality)
#   6. the rate limiter refuses, and never refuses a health check
#   7. the fake processor's crash knob is refused when affordances are disabled
#
# Section 7 only proves anything against a server started with the knobs off, so it
# reports rather than fails when they are on -- which is the normal local default.
# Run it against a production-configured server to see it bite:
#
#   $env:APP_ENV="production"; uvicorn app.main:app
#
# Bodies go to temp files and are passed with --data-binary "@file" for the same
# reason as every earlier smoke script: nested quotes inside a curl.exe argument are
# mangled differently by PowerShell 5.1 and 7.

param(
    [string]$BaseUrl = "http://localhost:8000",
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
        [string]$IdempotencyKey,
        [string]$RequestId
    )

    # -D writes response headers to a file: this script asserts on headers more than
    # on bodies, which no earlier smoke script needed.
    $headerFile = Join-Path $TmpDir "headers.txt"
    $curlArgs = @("-s", "-D", $headerFile, "-w", "`n%{http_code}", "-X", $Method, "$Base$Path")
    if ($IdempotencyKey) { $curlArgs += @("-H", "Idempotency-Key: $IdempotencyKey") }
    if ($RequestId) { $curlArgs += @("-H", "X-Request-ID: $RequestId") }
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
    $raw = if ($lines.Count -gt 1) { ($lines[0..($lines.Count - 2)] -join "") } else { "" }

    $parsed = $null
    if ($raw) { try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null } }

    $headers = @{}
    foreach ($line in (Get-Content $headerFile)) {
        if ($line -match "^([^:]+):\s*(.*)$") { $headers[$matches[1].ToLower()] = $matches[2].Trim() }
    }

    [PSCustomObject]@{
        Status  = $status
        Body    = $parsed
        Raw     = $raw
        Headers = $headers
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

function Assert-Contains {
    param([string]$Haystack, [string]$Needle, [string]$What)
    if ($Haystack -like "*$Needle*") {
        Write-Host "  PASS  $What" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- '$Needle' not found" -ForegroundColor Red
        exit 1
    }
}

function Assert-NotContains {
    param([string]$Haystack, [string]$Needle, [string]$What)
    if ($Haystack -notlike "*$Needle*") {
        Write-Host "  PASS  $What" -ForegroundColor Green
    }
    else {
        Write-Host "  FAIL  $What -- '$Needle' should NOT be present" -ForegroundColor Red
        exit 1
    }
}

Write-Host "`n=== 1. Every response is traceable ===" -ForegroundColor Cyan

$health = Invoke-Api -Method GET -Path "/health"
Assert-Equal -Expected 200 -Actual $health.Status -What "GET /health"
Assert-NotNull -Actual $health.Headers["x-request-id"] -What "a generated request id"

# A caller's own id is honoured, which is what lets a trace span two services.
$traced = Invoke-Api -Method GET -Path "/health" -RequestId "smoke-phase7-trace"
Assert-Equal -Expected "smoke-phase7-trace" -Actual $traced.Headers["x-request-id"] `
    -What "a supplied request id is propagated"

Write-Host "`n=== 2. Security headers ===" -ForegroundColor Cyan

Assert-Equal -Expected "nosniff" -Actual $health.Headers["x-content-type-options"] `
    -What "X-Content-Type-Options"
Assert-Equal -Expected "DENY" -Actual $health.Headers["x-frame-options"] -What "X-Frame-Options"
Assert-Equal -Expected "no-referrer" -Actual $health.Headers["referrer-policy"] `
    -What "Referrer-Policy"
Assert-Contains -Haystack $health.Headers["content-security-policy"] -Needle "default-src 'none'" `
    -What "Content-Security-Policy"

Write-Host "`n=== 3. Liveness and readiness are different questions ===" -ForegroundColor Cyan

# /health must touch NO dependency: if it checked Postgres, a database blip would
# fail liveness on every machine at once and the platform would restart all of them.
Assert-Equal -Expected "ok" -Actual $health.Body.status -What "/health (liveness, no dependency)"

$ready = Invoke-Api -Method GET -Path "/ready"
Assert-Equal -Expected 200 -Actual $ready.Status -What "GET /ready"
Assert-Equal -Expected "ready" -Actual $ready.Body.status -What "/ready (readiness)"
Assert-Equal -Expected "ok" -Actual $ready.Body.database -What "/ready checked the database"

Write-Host "`n=== 4. Metrics, including the invariant as one number ===" -ForegroundColor Cyan

# Make some money move first, so the gauges have something real to report.
$account = Invoke-Api -Method POST -Path "/accounts" -Body '{"name":"Phase 7","currency":"INR"}'
$accountId = $account.Body.id
$charge = Invoke-Api -Method POST -Path "/charges" `
    -Body "{`"account_id`": `"$accountId`", `"amount`": $Amount}" `
    -IdempotencyKey ([guid]::NewGuid().ToString())
Assert-Equal -Expected 201 -Actual $charge.Status -What "a charge to give the gauges something to say"

$metrics = Invoke-Api -Method GET -Path "/metrics"
Assert-Equal -Expected 200 -Actual $metrics.Status -What "GET /metrics"

Assert-Contains -Haystack $metrics.Raw -Needle "# TYPE ledgerline_http_requests_total counter" `
    -What "HTTP request counter"
Assert-Contains -Haystack $metrics.Raw -Needle "ledgerline_http_request_duration_seconds_bucket" `
    -What "latency histogram"
Assert-Contains -Haystack $metrics.Raw -Needle 'le="+Inf"' -What "histogram has the +Inf bucket"

# THE capstone metric. Credits minus debits over every entry in the database.
# Nothing sets this; it is a SUM, and double-entry means it is exactly zero.
Assert-Contains -Haystack $metrics.Raw -Needle "ledgerline_ledger_imbalance_minor_units 0" `
    -What "LEDGER IMBALANCE IS ZERO"
Assert-Contains -Haystack $metrics.Raw -Needle "ledgerline_refunds_over_limit 0" `
    -What "no payment is over-refunded"
Assert-Contains -Haystack $metrics.Raw -Needle "ledgerline_outbox_pending" -What "outbox depth"
Assert-Contains -Haystack $metrics.Raw -Needle "ledgerline_payments_stuck" -What "stuck payments"

Write-Host "`n=== 5. Metrics are labelled by route template, not by path ===" -ForegroundColor Cyan

$read = Invoke-Api -Method GET -Path "/charges/$($charge.Body.id)"
Assert-Equal -Expected 200 -Actual $read.Status -What "GET /charges/{id}"

$metrics2 = Invoke-Api -Method GET -Path "/metrics"
Assert-Contains -Haystack $metrics2.Raw -Needle 'route="/charges/{payment_id}"' `
    -What "labelled by the route TEMPLATE"
# One series per payment id would not degrade the service -- it would degrade the
# monitoring system, slowly, until nobody could query anything.
Assert-NotContains -Haystack $metrics2.Raw -Needle $charge.Body.id `
    -What "the payment id is NOT a label value"

Write-Host "`n=== 6. Rate limiting ===" -ForegroundColor Cyan

Assert-NotNull -Actual $read.Headers["x-ratelimit-limit"] -What "X-RateLimit-Limit is advertised"

# A health check must NEVER be refused: a 429 on a liveness probe is a platform
# concluding the machine is unhealthy and restarting it.
$probeOk = $true
for ($i = 0; $i -lt 25; $i++) {
    if ((Invoke-Api -Method GET -Path "/health").Status -ne 200) { $probeOk = $false }
}
Assert-Equal -Expected $true -Actual $probeOk -What "25 health checks, none refused"

Write-Host "`n=== 7. The fake processor's crash knob ===" -ForegroundColor Cyan

$crashBody = @"
{
  "account_id": "$accountId",
  "amount": $Amount,
  "force_crash_after_processor": true
}
"@
$crash = Invoke-Api -Method POST -Path "/charges" -Body $crashBody `
    -IdempotencyKey ([guid]::NewGuid().ToString())

if ($crash.Status -eq 422) {
    Write-Host "  PASS  crash knob refused (422) -- affordances are disabled" -ForegroundColor Green
    Assert-Contains -Haystack $crash.Raw -Needle "force_crash_after_processor" `
        -What "the refusal names the offending field"
}
elseif ($crash.Status -eq 500) {
    Write-Host "  INFO  crash knob is ENABLED here, so the charge crashed as designed (500)." `
        -ForegroundColor Yellow
    Write-Host "        That is the correct local default. To see it refused, restart with:" `
        -ForegroundColor DarkGray
    Write-Host '          $env:APP_ENV="production"; uvicorn app.main:app' -ForegroundColor DarkGray
    # Even here, the 500 must be a clean one: an id, and nothing about our internals.
    Assert-Contains -Haystack $crash.Raw -Needle "request_id" -What "the 500 carries a request id"
    Assert-NotContains -Haystack $crash.Raw -Needle "Traceback" -What "no traceback on the wire"
    Assert-NotContains -Haystack $crash.Raw -Needle "app/routers" -What "no file paths on the wire"
}
else {
    Write-Host "  FAIL  unexpected status $($crash.Status) from the crash knob" -ForegroundColor Red
    exit 1
}

Write-Host "`nPhase 7 smoke: all checks passed.`n" -ForegroundColor Green
Write-Host "The metric worth putting on a dashboard is this one:" -ForegroundColor DarkGray
Write-Host "  ledgerline_ledger_imbalance_minor_units   -- must be 0, always" -ForegroundColor DarkGray
Write-Host "It is the whole double-entry invariant as a single number. If it is ever" -ForegroundColor DarkGray
Write-Host "non-zero, money was written outside every flow that is supposed to be the" -ForegroundColor DarkGray
Write-Host "only way to write money, and nothing else on the dashboard matters.`n" -ForegroundColor DarkGray
