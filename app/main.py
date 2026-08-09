from fastapi import FastAPI

from app.routers import accounts, transactions

app = FastAPI(title="Ledgerline")

app.include_router(accounts.router)
app.include_router(transactions.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
