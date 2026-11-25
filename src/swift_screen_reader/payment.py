"""Card payment terminals. Only a simulator for now; a real terminal will implement `PaymentTerminal`."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from decimal import Decimal

APPROVED = "approved"
DECLINED = "declined"
ERROR = "error"

SIMULATED = "simulated"
TERMINALS = {SIMULATED: "Simulated terminal (always approves)"}


@dataclass(frozen=True)
class PaymentResult:
    status: str
    message: str
    auth_code: str = ""
    terminal_reference: str = ""

    @property
    def approved(self) -> bool:
        return self.status == APPROVED


class PaymentTerminal:
    """Interface for a card terminal. `charge` blocks (it runs on a worker thread) and must not raise."""

    name = "terminal"

    def charge(self, amount: Decimal, currency: str, reference: str) -> PaymentResult:
        raise NotImplementedError


class SimulatedTerminal(PaymentTerminal):
    name = "Simulated terminal"

    def __init__(self, delay_seconds: float = 2.0) -> None:
        self.delay_seconds = delay_seconds

    def charge(self, amount: Decimal, currency: str, reference: str) -> PaymentResult:
        time.sleep(max(0.0, self.delay_seconds))
        return PaymentResult(
            APPROVED,
            "Transaction successful",
            auth_code=f"{random.randint(0, 999999):06d}",
            terminal_reference=f"SIM-{reference}",
        )


def create_terminal(kind: str, delay_seconds: float) -> PaymentTerminal:
    if kind == SIMULATED:
        return SimulatedTerminal(delay_seconds)
    raise ValueError(f"Unknown payment terminal {kind!r}")
