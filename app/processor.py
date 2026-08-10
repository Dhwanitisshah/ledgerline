"""A fake payment processor.

Phase 2 needs a processor that fails **on demand**. That is the entire point: the
guarantee this phase is built around -- a failed charge moves no money -- is
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
"""

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProcessorOutcome(StrEnum):
    """What the processor said. Deliberately binary -- there is no 'pending'.

    Real processors have asynchronous outcomes: they accept the charge, go away,
    and resolve it later over a webhook. Modelling that needs the webhook and
    outbox machinery of Phase 5, so Phase 2 stays synchronous -- the adapter has
    an answer before it returns, and the charge flow can rely on that.
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
    """The seam. One method, because Phase 2 needs exactly one."""

    async def charge(self, amount: int, currency: str) -> ChargeResult: ...


class FakeProcessor:
    """A processor whose answer is decided by configuration, not by a network."""

    def __init__(
        self,
        *,
        outcome: ProcessorOutcome = ProcessorOutcome.SUCCESS,
        latency_ms: int = 0,
    ) -> None:
        self.outcome = outcome
        self.latency_ms = latency_ms

    async def charge(self, amount: int, currency: str) -> ChargeResult:
        """Return the configured outcome, after the configured delay.

        The delay is not decoration. The charge flow calls this with a database
        transaction already open, so ``latency_ms`` is the knob that makes the
        cost of that visible: every millisecond spent in here is a millisecond a
        Postgres connection sits idle inside a write transaction, holding its
        locks. Phase 4 is where that stops being a curiosity.
        """
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000)

        processor_ref = f"fake_ch_{uuid.uuid4().hex[:24]}"

        if self.outcome is ProcessorOutcome.FAILURE:
            return ChargeResult(
                outcome=ProcessorOutcome.FAILURE,
                processor_ref=processor_ref,
                failure_reason="card_declined (forced by the fake processor)",
            )

        return ChargeResult(outcome=ProcessorOutcome.SUCCESS, processor_ref=processor_ref)
