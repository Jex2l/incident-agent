"""Hard budgets for the investigation loop: max tool-call steps, max tokens,
and a wall-clock timeout. The loop checks these every iteration and must
stop (returning "inconclusive") rather than run unbounded.

This is the agent's self-preservation mechanism: a local 7B model can get
stuck re-querying the same tool, or simply take a long time on
resource-constrained hardware. Without these caps a single bad investigation
could run indefinitely and never report anything back to the on-call human.
"""
import os
import time
from dataclasses import dataclass, field

# All three caps are configurable via environment variables (see .env.example)
# so they can be tuned per-deployment without touching code. Defaults were
# raised during development after real runs on constrained hardware blew
# through the original, tighter defaults (see docs/DECISIONS.md).
DEFAULT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))
DEFAULT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16000"))
DEFAULT_WALLCLOCK_TIMEOUT_S = float(os.environ.get("AGENT_WALLCLOCK_TIMEOUT_S", "480"))


@dataclass
class Budget:
    """Immutable configuration: the limits for a single investigation.

    Constructed once per investigation (usually from the module-level
    defaults above) and handed to a BudgetTracker, which does the actual
    counting.
    """
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = DEFAULT_MAX_TOKENS
    wallclock_timeout_s: float = DEFAULT_WALLCLOCK_TIMEOUT_S


@dataclass
class BudgetTracker:
    """Mutable, per-investigation counter that the orchestrator loop updates
    after every LLM call and tool call, and consults before starting the
    next step.
    """
    budget: Budget
    steps_used: int = 0
    tokens_used: int = 0
    # Wall-clock start time, captured the moment this tracker is created
    # (i.e. when the investigation begins). `time.monotonic()` is used
    # instead of `time.time()` because it can't go backwards if the system
    # clock is adjusted mid-run.
    _start: float = field(default_factory=time.monotonic)

    def record_step(self):
        """Call once per tool call (or LLM turn) the orchestrator performs."""
        self.steps_used += 1

    def record_tokens(self, prompt_tokens: int, completion_tokens: int):
        """Call after every LLM response to accumulate total token spend."""
        self.tokens_used += prompt_tokens + completion_tokens

    @property
    def elapsed_s(self) -> float:
        """Seconds since this tracker (i.e. the investigation) started."""
        return time.monotonic() - self._start

    def exceeded(self) -> str | None:
        """Returns the reason budget was exceeded, or None if still within budget.

        Checked at the top of every orchestrator loop iteration; the first
        limit that trips wins (order here doesn't matter much in practice
        since any one of them stops the loop the same way).
        """
        if self.steps_used >= self.budget.max_steps:
            return f"max_steps ({self.budget.max_steps}) reached"
        if self.tokens_used >= self.budget.max_tokens:
            return f"max_tokens ({self.budget.max_tokens}) reached"
        if self.elapsed_s >= self.budget.wallclock_timeout_s:
            return f"wallclock_timeout_s ({self.budget.wallclock_timeout_s}) reached"
        return None

    def summary(self) -> dict:
        """Snapshot used for logging/eval reporting - how much of the budget
        a finished (or aborted) investigation actually consumed."""
        return {
            "steps_used": self.steps_used,
            "tokens_used": self.tokens_used,
            "elapsed_s": round(self.elapsed_s, 2),
        }
