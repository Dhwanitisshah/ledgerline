# Phase 1: the money model

Phase 1 is a ledger and nothing else. No charge flow, no processor, no
idempotency keys, no webhooks — those are later phases. What it does establish is
the set of rules everything afterwards has to obey.

### Money is an integer, everywhere

Amounts are `BIGINT` **minor units**: paise for INR, cents for USD. `2500.00 INR`
is the integer `250000`. There is no float and no `Decimal` at any layer — not in
the column type, not in Python, not on the wire.

The API boundary enforces this rather than assuming it. Request amounts are typed
`StrictInt`, so a JSON body containing `250000.0` or `"250000"` is rejected with a
422 instead of being quietly coerced to `250000`. A rounding bug you never write
is one you never have to find.

There is one subtlety worth naming: `SUM()` over a `bigint` column returns
`numeric` in Postgres, which asyncpg hands back as a Python `Decimal`. Every SUM
in [app/ledger.py](../app/ledger.py) is therefore cast back with `::bigint`, so money
does not silently change type on the way out of the database.

### Balances are derived, never stored

`accounts` has **no balance column**. Look at the table; there is nowhere to put
one. A balance is always computed with a SQL SUM over `ledger_entries`:

```sql
SELECT COALESCE(
    SUM(CASE direction
            WHEN 'credit' THEN amount
            WHEN 'debit'  THEN -amount
        END),
    0
)::bigint
FROM ledger_entries
WHERE account_id = :account_id
```

**Balance convention, used everywhere without exception:**

```
balance = SUM(credit amounts) - SUM(debit amounts)
```

Crediting an account raises its balance; debiting it lowers it. Which convention
you pick matters far less than applying one consistently, so it is stated once in
[app/ledger.py](../app/ledger.py), repeated here, and never re-decided inside a route.

A stored balance is a cached total that has to be kept in step with the entries
that justify it. The moment a crash, a retry, or a race lands between the two
writes, the balance and its own audit trail disagree — and the balance is the one
customers see. Deriving it means the number and its explanation cannot drift,
because they are the same thing.

### The ledger is append-only, and Postgres enforces it

Every posting is a `ledger_transaction` grouping two or more `ledger_entries`.
Entries carry a positive `amount` and a `direction` of `debit` or `credit`; the
sign lives in the direction, so `amount > 0` is a simple CHECK constraint and a
negative credit is not representable.

Nothing in the application ever issues an `UPDATE` or `DELETE` against those
tables — but "the application never does it" is a convention, and conventions get
broken by a data-fix script, a psql session, or a teammate in six months. So
migration [0002](../alembic/versions/0002_ledger.py) installs triggers that make the
database refuse:

```sql
CREATE TRIGGER ledger_entries_immutable
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH STATEMENT EXECUTE FUNCTION ledgerline_forbid_mutation();
```

Try it against a running database:

```powershell
docker compose exec postgres psql -U postgres -d ledgerline -c "UPDATE ledger_entries SET amount = 1;"
# ERROR: ledgerline: UPDATE on ledger_entries is not permitted -- the ledger is append-only
```

Two deliberate choices here:

- **`FOR EACH STATEMENT`, not `FOR EACH ROW`.** A row-level trigger only fires for
  rows the statement actually matched, so `DELETE FROM ledger_entries WHERE false`
  would report success and teach you the wrong lesson. A statement-level trigger
  rejects the operation unconditionally.
- **TRUNCATE is not blocked.** TRUNCATE does not fire UPDATE or DELETE triggers,
  and the test suite uses it to reset state between tests. It is the one sanctioned
  way to clear the ledger, and it exists for tests only.

Corrections, when they come in a later phase, are made the way real ledgers make
them: by posting a compensating entry, not by editing history.

### Every transaction must sum to zero

Within one `ledger_transaction`, total debits must equal total credits. This is
checked in [app/routers/transactions.py](../app/routers/transactions.py) **inside the
database transaction, before commit**:

1. Write every entry.
2. `FLUSH`, then ask the database to total the debits and credits it now holds.
3. Commit only if they match; otherwise `ROLLBACK` and return 422.

Step 2 reads the totals back out of the database rather than re-adding the numbers
in the request body. The thing being verified is the state that would actually be
committed, not the intent that produced it — if serialization mangled something
between the two, this catches it.

Because the check runs before `COMMIT`, a rejected posting leaves nothing behind:
no orphaned transaction row, no half-written entry. Empty postings are rejected at
the schema, and single-sided postings fail the sum check with a distinct message.

**All entries in one transaction must also share a single currency**, resolved
from the `accounts` rows the entries point at — checked *before* the sum, because
summing 100 paise against 100 cents is meaningless arithmetic rather than a near
miss. A mixed-currency posting rolls back with a 422. This is the whole rule:
there is no FX, no conversion, and no currency column on transactions.

### Endpoints

| Method | Path                        | Behaviour                                                   |
| ------ | --------------------------- | ----------------------------------------------------------- |
| `POST` | `/accounts`                 | `{name, currency?}` → 201 with the created account           |
| `GET`  | `/accounts/{id}/balance`    | `{account_id, currency, balance}` derived by SQL SUM; 404 if unknown |
| `POST` | `/transactions`             | Posts a balanced entry set atomically; 422 if unbalanced     |

Balances are in minor units. `POST /transactions` also returns 422 for a posting
that mixes currencies, or for an entry referencing an account that does not exist.

### Not in Phase 1 (on purpose)

Idempotency keys (Phase 3), concurrency and row locking (Phase 4), the real charge
flow and processor adapter (Phase 2), webhooks and the outbox (Phase 5). There are
no overdraw guards yet — an account can go arbitrarily negative, which is correct
for a ledger that does not yet model credit limits.

`payments` existed as an empty placeholder table so the schema was complete. It had
no lifecycle, no status machine, and nothing read or wrote it — until Phase 2.

## Smoke acceptance criteria

- Balanced transfer moves both derived balances correctly (no stored balance)
- Unbalanced posting → 422 and zero rows written
- `UPDATE`/`DELETE` on `ledger_entries` → raises at the database
- `pytest` green locally and in CI


---

**[All phases](../README.md#the-phases)** · **Next:** [Phase 2: the payment lifecycle](phase-2-payment-lifecycle.md) →
