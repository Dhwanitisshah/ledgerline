from fastapi import FastAPI

from app.routers import accounts, charges, refunds, transactions, webhooks, withdrawals

app = FastAPI(title="Ledgerline")

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(charges.router)
# A second router on the /charges prefix: a refund is an operation *on a charge*
# and its URL says so, but the charge flow's file is long enough already.
app.include_router(refunds.router)
app.include_router(withdrawals.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
