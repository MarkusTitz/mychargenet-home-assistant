"""EDGE-HYBRID-STEUERHOHEIT1 (P5, nur Simulation): lokales Befehls-Gate der Edge.

Es gibt auf der Edge (noch) keinen lokalen OCPP-Steuerweg. Dieses Modul ist das Gate, das ein kuenftiger lokaler
Schreiber zwingend durchlaufen muss; heute wird es ausschliesslich gegen simulierte Ladepunkte getestet.

Regeln (fail-closed):
- Ohne gueltigen Lease kein Befehl. Der Lease liegt nur im Speicher: nach einem Neustart gibt es keine Steuerung,
  bis die Cloud den Lease bestaetigt (kein Fortsetzen nach Neustart).
- Ablauf ueber die monotone Uhr: Restlaufzeit kommt vom Server (expires_in_seconds), verankert am monotonen Zeitpunkt
  VOR dem Senden der Anfrage, abzueglich Sicherheitsmarge - die Wanduhr der Edge spielt keine Rolle (P5-Review B-3).
- Jeder Befehl traegt die Generation, unter der er entstanden ist; ein Befehl einer anderen Generation oder aus der
  Zeit vor der Lease-Vergabe wird nie gesendet. Grenze: Innerhalb DERSELBEN Generation (Verlaengerung) schuetzt nur
  created_before_lease; Befehle aus einer kurzen Partition derselben Generation gelten als aktuell.
- Jedes Limit endlich, >= 0 und <= Budget; Summenleistung nie ueber der verifizierten statischen Budgetgrenze.
- Fallback nach Ablauf: der Schreiber hoert auf; das bisher wirksame Limit wird nie erhoeht (fallback_limit_w).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime


SAFETY_MARGIN_SECONDS = 5.0


@dataclass(frozen=True)
class LocalLease:
    generation: int
    budget_w: int
    deadline_monotonic: float
    granted_monotonic: float
    answered_for_sent_monotonic: float = 0.0  # Sendezeitpunkt der Anfrage, deren Antwort zuletzt angenommen wurde


@dataclass(frozen=True)
class LocalCommand:
    charge_point: str
    limit_w: float
    generation: int
    created_monotonic: float


class LocalControlGate:
    def __init__(self, *, own_holder: str, clock=time.monotonic):
        """Eine Gate-Instanz je Edge-Bindung (Standort). own_holder ist Pflicht, z. B. "edge:7": nur Antworten fuer genau
        diese Edge zaehlen (P5-Review N-1). Bei Standortwechsel/Neukopplung eine neue Instanz verwenden."""
        if not isinstance(own_holder, str) or not re.fullmatch(r"edge:[0-9]+", own_holder):
            raise ValueError("own_holder muss 'edge:<id>' sein")
        self._clock = clock
        self._own_holder = own_holder
        self._lease: LocalLease | None = None
        self.effective_limits_w: dict[str, float] = {}
        # Codex-Reproducer (verspaetete Antworten): hoechste je gesehene Generation und Sperrgrenze nach Verlust/Rueckgabe.
        # Eine aeltere Generation wird nie (wieder) angenommen; nach drop/Rueckgabe auch nicht die zuletzt gehaltene.
        self._highest_generation = 0
        self._fenced_through = 0

    @property
    def lease(self) -> LocalLease | None:
        return self._lease

    def accept_lease(self, answer: dict, sent_monotonic: float) -> LocalLease | None:
        """Lease-Antwort der Cloud uebernehmen. sent_monotonic = monotone Zeit unmittelbar VOR dem Senden der Anfrage:
        die Laufzeit der Antwort wird so nie gutgeschrieben. Restlaufzeit ausschliesslich vom Server, minus Marge.
        Antwort ohne Edge-Hoheit oder ohne gueltige Restlaufzeit loescht den lokalen Lease (fail-closed)."""
        remaining = answer.get("expires_in_seconds")
        budget = answer.get("budget_w")
        generation = answer.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            self.drop_lease()
            return None
        holder = str(answer.get("holder", ""))
        if holder != self._own_holder:
            # "nicht diese Edge" wird immer befolgt (sichere Richtung), auch wenn die Antwort keine Generation verraet.
            self._highest_generation = max(self._highest_generation, generation)
            self.drop_lease()
            return None
        if generation < self._highest_generation:
            return self._lease  # verspaetete Antwort einer aelteren Generation: nie annehmen, aktuellen Zustand behalten
        if self._lease is None and generation <= self._fenced_through:
            return None  # nach Verlust/Rueckgabe belebt keine ausstehende Antwort derselben Generation die Hoheit wieder
        if (not isinstance(remaining, (int, float)) or isinstance(remaining, bool) or not math.isfinite(remaining)
                or not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0):
            self.drop_lease()
            return None
        remaining = float(remaining) - SAFETY_MARGIN_SECONDS
        if remaining <= 0:
            self.drop_lease()
            return None
        previous = self._lease
        same = previous is not None and previous.generation == generation
        if same and sent_monotonic < previous.answered_for_sent_monotonic:
            return previous  # verspaetete Antwort einer frueheren Anfrage derselben Generation: nie verlaengern
        granted = previous.granted_monotonic if same else sent_monotonic
        self._highest_generation = generation
        self._lease = LocalLease(generation=generation, budget_w=budget, deadline_monotonic=sent_monotonic + remaining,
                                 granted_monotonic=granted, answered_for_sent_monotonic=sent_monotonic)
        return self._lease

    def drop_lease(self) -> None:
        if self._lease is not None:
            self._fenced_through = max(self._fenced_through, self._lease.generation)
        self._fenced_through = max(self._fenced_through, self._highest_generation)
        self._lease = None

    def admit(self, command: LocalCommand) -> str | None:
        """None = senden erlaubt, sonst Ablehnungsgrund. Prueft auch die Summenleistung inkl. dieses Befehls."""
        lease = self._lease
        if lease is None:
            return "no_lease"
        if self._clock() >= lease.deadline_monotonic:
            return "lease_expired"
        if command.generation != lease.generation:
            return "stale_generation"
        if command.created_monotonic < lease.granted_monotonic:
            return "created_before_lease"
        limit = command.limit_w
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0 \
                or limit > lease.budget_w:
            return "invalid_limit"  # NaN/inf/negativ/ueber Budget je Befehl (P5-Review B-4)
        total = sum(value for key, value in self.effective_limits_w.items() if key != command.charge_point) + limit
        if total > lease.budget_w:
            return "over_budget"
        return None

    def apply(self, command: LocalCommand, send) -> str | None:
        """Gate + simulierter Sendeweg. send(charge_point, limit_w) wird nur bei zugelassenem Befehl aufgerufen."""
        reason = self.admit(command)
        if reason is None:
            send(command.charge_point, command.limit_w)
            self.effective_limits_w[command.charge_point] = command.limit_w
        return reason

    def fallback_limit_w(self, charge_point: str, failsafe_w: float) -> float:
        """Fallback nach Lease-Verlust: nie hoeher als das bisher wirksame Limit (und nie ueber dem Failsafe-Wert)."""
        current = self.effective_limits_w.get(charge_point)
        return failsafe_w if current is None else min(current, failsafe_w)


def utc_now() -> datetime:
    return datetime.now(UTC)
