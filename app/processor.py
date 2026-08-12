"""A fake payment processor, and the books it keeps.

Phase 2 needs a processor that fails **on demand**. That is the entire point: the
guarantee that phase is built around -- a failed charge moves no money -- is
untestable if the only way to observe a failure is to wait for a real one.

So there is no network here, no API key, and no SDK. :class:`FakeProcessor`
returns the outcome it was configured to return, after the delay it was
configured to wait. Configuration arrives from two places:

* environment / ``.env`` -- ``PROCESSOR_OUTCOME`` and ``PROCESSOR_LATENCY_MS``,
  read into :class:`app.config.Settings` and applied to every charge;
* the request body -- ``force_outcome`` and ``force_latency_ms``, overriding the
  settings for a single call. That is what lets the smoke script exercise the
  failure path against a running server without restarting it under different
  environment variables.

:class:`ProcessorAdapter` is the seam a real processor would later slot into.
Nothing outside this module knows that the processor is fake.

## What Phase 5a adds, and why it is not a detail

Through Phase 4 the processor was amnesiac: it invented a reference, returned it,
and forgot the charge had happened. That is fine right up until the question
Phase 5a has to answer, which is *"we crashed -- did the card get charged or
not?"*. An amnesiac processor makes that question unanswerable, and a gap you
cannot interrogate is a gap you can only write prose about.

So the fake now keeps books, in :class:`ProcessorBooks`, and two properties of
how it does that carry the whole phase:

1. **It writes on its own session, in its own transaction.** Not the caller's.
   When Ledgerline's charge transaction rolls back -- because the process died,
   because an invariant failed, because anything -- the processor's row stays
   committed. That asymmetry is not a quirk of the fake; it is the actual shape of
   the problem. The card issuer does not roll back because your web process
   segfaulted.
2. **A charge is keyed by ``attempt_ref`` and is idempotent on it.** Charging the
   same reference twice returns the first outcome instead of taking the money
   again. Real processors behave this way when you send an idempotency key, and it
   is what makes :meth:`ProcessorAdapter.lookup` safe for the sweep to lean on.

If a real adapter ever lands, ``ProcessorBooks`` is deleted outright: the real
processor keeps its own books on its own servers, and ``lookup`` becomes an HTTP
GET against them. The seam is already the right shape for that.
"""

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ProcessorOutcome(StrEnum):
    """What the processor said. Deliberately binary -- there is no 'pending'.

    Real processors have asynchronous outcomes: they accept the charge, go away,
    and resolve it later over a webhook. Modelling that needs the webhook
    machinery of Phase 5b, so the adapter stays synchronous -- it has an answer
    before it returns, and the charge flow can rely on that.

    Note what this does *not* mean. A synchronous adapter still leaves the
    possibility that Ledgerline never learns the answer, because the answer can be
    lost on the way back: the process can die between the return and the commit.
    "The processor is synchronous" and "Ledgerline knows the outcome" are
    different claims, and Phase 5a is about the gap between them.
    """

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class ChargeResult:
    """The processor's answer for one charge attempt.

    ``processor_ref`` is populated for failures too. Real processors issue a
    reference for a declined charge, and it is the only handle anyone has when a
    customer asks why their card was refused -- so discarding it on the failure
    path would be throwing away the thing most wanted during an incident.
    """

    outcome: ProcessorOutcome
    processor_ref: str
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ProcessorOutcome.SUCCESS


class ProcessorAdapter(Protocol):
    """The seam. Two methods as of Phase 5a.

    ``lookup`` is the one that closes the crash window, and it is worth being
    explicit that this is a *requirement placed on the processor*, not something
    Ledgerline can implement on its own. If a processor offers no way to ask "what
    happened to the attempt I identified as X?", then an outcome lost in transit is
    lost permanently, and no amount of care on this side recovers it. The gap can
    then only be narrowed, never closed. Every serious processor offers it; the
    point is that the guarantee is jointly held.
    """

    async def charge(
        self, *, attempt_ref: uuid.UUID, amount: int, currency: str
    ) -> ChargeResult: ...

    async def lookup(self, attempt_ref: uuid.UUID) -> ChargeResult | None: ...


# --- The processor's own database ----------------------------------------------

_RECORD_CHARGE_SQL = text(
    """
    INSERT INTO processor_charges (
        attempt_ref, processor_ref, amount, currency, outcome, failure_reason
    )
    VALUES (
        :attempt_ref, :processor_ref, :amount, :currency, :outcome, :failure_reason
    )
    ON CONFLICT (attempt_ref) DO NOTHING
    """
)

_READ_CHARGE_SQL = text(
    """
    SELECT processor_ref, outcome, failure_reason
    FROM processor_charges
    WHERE attempt_ref = :attempt_ref
    """
)


class ProcessorBooks:
    """Where the fake processor records what it charged.

    Every method here opens its **own** session from the factory it was given,
    rather than accepting one from the caller. That is the design, not laziness
    about plumbing: a caller-supplied session would make the processor's records
    part of Ledgerline's transaction, they would roll back together, and the crash
    this phase exists to survive would quietly stop being survivable. Test it by
    deleting the word ``own`` from this paragraph and watching
    ``test_the_single_txn_charge_loses_a_charged_card`` stop reproducing.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        attempt_ref: uuid.UUID,
        processor_ref: str,
        amount: int,
        currency: str,
        outcome: ProcessorOutcome,
        failure_reason: str | None,
    ) -> ChargeResult:
        """Write the charge down and return what the books now say.

        ``ON CONFLICT DO NOTHING`` followed by a read is the idempotency: a repeat
        attempt on the same reference writes nothing and returns the *original*
        outcome, so a retried charge cannot take the money twice and cannot change
        its own history. The read is what makes that observable -- returning the
        arguments instead would report the outcome this call wanted rather than the
        one that stands.
        """
        async with self._session_factory() as session:
            await session.execute(
                _RECORD_CHARGE_SQL,
                {
                    "attempt_ref": attempt_ref,
                    "processor_ref": processor_ref,
                    "amount": amount,
                    "currency": currency,
                    "outcome": outcome.value,
                    "failure_reason": failure_reason,
                },
            )
            row = (await session.execute(_READ_CHARGE_SQL, {"attempt_ref": attempt_ref})).one()
            # The processor commits on its own schedule and nobody else's. By the
            # time this returns, the charge is a fact outside Ledgerline's control.
            await session.commit()

        return ChargeResult(
            outcome=ProcessorOutcome(row.outcome),
            processor_ref=row.processor_ref,
            failure_reason=row.failure_reason,
        )

    async def find(self, attempt_ref: uuid.UUID) -> ChargeResult | None:
        """What the processor knows about one attempt, or None if it never saw it.

        None is a genuine answer and the sweep depends on it being trustworthy: it
        means the charge never reached the processor, so no money moved, so the
        payment can be settled as failed without anyone being out of pocket.
        """
        async with self._session_factory() as session:
            row = (await session.execute(_READ_CHARGE_SQL, {"attempt_ref": attempt_ref})).first()

        if row is None:
            return None

        return ChargeResult(
            outcome=ProcessorOutcome(row.outcome),
            processor_ref=row.processor_ref,
            failure_reason=row.failure_reason,
        )


class FakeProcessor:
    """A processor whose answer is decided by configuration, not by a network."""

    def __init__(
        self,
        *,
        outcome: ProcessorOutcome = ProcessorOutcome.SUCCESS,
        latency_ms: int = 0,
        books: ProcessorBooks | None = None,
    ) -> None:
        self.outcome = outcome
        self.latency_ms = latency_ms
        # None means an amnesiac processor: it answers, and forgets. Kept as an
        # option because a handful of tests care only about the answer, and because
        # it is the honest way to model a processor with no lookup API -- against
        # which, as ProcessorAdapter says, the crash window cannot be closed.
        self.books = books

    async def charge(
        self, *, attempt_ref: uuid.UUID, amount: int, currency: str
    ) -> ChargeResult:
        """Return the configured outcome, after the configured delay.

        The delay is not decoration. Under ``CHARGE_DURABILITY=single_txn`` the
        charge flow calls this with a database transaction already open, so
        ``latency_ms`` is the knob that makes the cost of that visible: every
        millisecond spent here is a millisecond a Postgres connection sits idle
        inside a write transaction, holding its locks. Under ``durable_intent``
        that is no longer true -- the intent has already been committed and no
        transaction is open across this call, which is a second thing the Phase 5a
        split buys and the README counts.
        """
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)

        processor_ref = f"fake_ch_{uuid.uuid4().hex[:24]}"
        failure_reason = (
            "card_declined (forced by the fake processor)"
            if self.outcome is ProcessorOutcome.FAILURE
            else None
        )

        if self.books is None:
            return ChargeResult(
                outcome=self.outcome,
                processor_ref=processor_ref,
                failure_reason=failure_reason,
            )

        # Committed here, independently, before this function returns. Everything
        # after this point on the caller's side can fail without un-charging it.
        return await self.books.record(
            attempt_ref=attempt_ref,
            processor_ref=processor_ref,
            amount=amount,
            currency=currency,
            outcome=self.outcome,
            failure_reason=failure_reason,
        )

    async def lookup(self, attempt_ref: uuid.UUID) -> ChargeResult | None:
        """Ask the processor what became of one attempt."""
        if self.books is None:
            return None
        return await self.books.find(attempt_ref)
