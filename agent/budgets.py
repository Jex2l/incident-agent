"""Hard budgets for the investigation loop: max tool-call steps, max tokens,
and a wall-clock timeout. The loop checks these every iteration and must
stop (returning "inconclusive") rather than run unbounded.
"""
import os
import time
from dataclasses import dataclass, field

DEFAULT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "12"))
DEFAULT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16000"))
DEFAULT_WALLCLOCK_TIMEOUT_S = float(os.environ.get("AGENT_WALLCLOCK_TIMEOUT_S", "480"))


@dataclass
class Budget:
    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int = DEFAULT_MAX_TOKENS
    wallclock_timeout_s: float = DEFAULT_WALLCLOCK_TIMEOUT_S


@dataclass
class BudgetTracker:
    budget: Budget
    steps_used: int = 0
    tokens_used: int = 0
    _start: float = field(default_factory=time.monotonic)

    def record_step(self):
        self.steps_used += 1

    def record_tokens(self, prompt_tokens: int, completion_tokens: int):
        self.tokens_used += prompt_tokens + completion_tokens

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._start

    def exceeded(self) -> str | None:
        """Returns the reason budget was exceeded, or None if still within budget."""
        if self.steps_used >= self.budget.max_steps:
            return f"max_steps ({self.budget.max_steps}) reached"
        if self.tokens_used >= self.budget.max_tokens:
            return f"max_tokens ({self.budget.max_tokens}) reached"
        if self.elapsed_s >= self.budget.wallclock_timeout_s:
            return f"wallclock_timeout_s ({self.budget.wallclock_timeout_s}) reached"
        return None

    def summary(self) -> dict:
        return {
            "steps_used": self.steps_used,
            "tokens_used": self.tokens_used,
            "elapsed_s": round(self.elapsed_s, 2),
        }
