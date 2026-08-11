from fastapi import FastAPI

from app.routers import accounts, charges, transactions, withdrawals

app = FastAPI(title="Ledgerline")

app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(charges.router)
app.include_router(withdrawals.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
