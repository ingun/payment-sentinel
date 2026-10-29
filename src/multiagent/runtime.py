"""
Çoklu agent çalışma zamanı: paylaşılan kanıt deposu ve sınırlı araç çalıştırıcı.

- EvidenceStore: her araç çağrısını "E<n>" referansıyla kaydeder. Kayıt biçimi tek agent executor'ıyla
  aynıdır; böylece risk motoru ve rapor üretici değişmeden kullanılır.
- ToolRunner: agent'a özgü araç izin listesi, toplam araç çağrısı bütçesi, araç başına timeout,
  geçici hatada sınırlı tekrar ve çıktı şeması doğrulaması uygular. Araçlar thread'de çalışır; bu sayede
  bağımsız agent'lar asyncio ile eşzamanlı yürütülebilir.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from pydantic import ValidationError

from src.agent import _args_scope, summarize_for_trace
from src.multiagent.contracts import ExecutionBudget
from src.repositories import DataSourceError, QueryLimitError
from src.tools import PaymentToolkit, ToolError, TransientToolError
from src.validation import InputValidationError, validate_tool_output

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Toplam araç çağrısı bütçesi doldu."""


class ToolNotAllowed(PermissionError):
    """Agent, izin listesinde olmayan bir aracı çağırmaya çalıştı."""


class EvidenceStore:
    def __init__(self) -> None:
        self.results: list[dict] = []

    def record(self, entry: dict) -> str:
        ref = f"E{len(self.results) + 1}"
        self.results.append({**entry, "ref": ref})
        return ref

    def get(self, ref: str) -> dict | None:
        return next((r for r in self.results if r["ref"] == ref), None)

    def is_valid(self, ref: str) -> bool:
        entry = self.get(ref)
        return entry is not None and entry.get("status") == "ok"

    def ok_refs(self) -> list[str]:
        return [r["ref"] for r in self.results if r.get("status") == "ok"]


class ToolRunner:
    def __init__(self, toolkit: PaymentToolkit, store: EvidenceStore, budget: ExecutionBudget,
                 emit: Callable[[dict], None] | None = None):
        self.toolkit = toolkit
        self.store = store
        self.budget = budget
        self.emit = emit or (lambda event: None)
        self.calls = 0

    @property
    def remaining(self) -> int:
        return max(0, self.budget.max_tool_calls - self.calls)

    async def call(self, agent: str, allowed: frozenset[str], tool: str, args: dict, reason: str,
                   round_no: int) -> tuple[dict | None, str]:
        """Aracı çalıştırır ve (çıktı veya None, kanıt referansı) döner. Bütçe dolarsa BudgetExceeded fırlatır."""
        if tool not in allowed:
            raise ToolNotAllowed(f"{agent} aracı çağıramaz: {tool}")
        if self.calls >= self.budget.max_tool_calls:
            self.emit({"kind": "limit", "node": agent, "round": round_no,
                       "detail": f"Araç çağrısı bütçesi ({self.budget.max_tool_calls}) doldu; {tool} çalıştırılmadı."})
            raise BudgetExceeded(f"Araç çağrısı bütçesi doldu ({self.budget.max_tool_calls}).")
        self.calls += 1  # olay döngüsü tek thread'de olduğu için sayaç yarışsızdır
        step = self.calls
        started = time.perf_counter()
        entry = {"step": step, "agent": agent, "round": round_no, "tool": tool, "args": args,
                 "scope": _args_scope(args), "decision_reason": reason}
        attempts = 0
        output = None
        while True:
            attempts += 1
            try:
                output = await asyncio.wait_for(asyncio.to_thread(self.toolkit.call, tool, args), self.budget.tool_timeout_s)
                issues = validate_tool_output(tool, output)
                if issues:
                    raise ToolError("Hatalı biçimli araç çıktısı: " + "; ".join(issues[:3]))
                entry.update(status="ok", output=output, summary=summarize_for_trace(tool, output))
                break
            except (TransientToolError, asyncio.TimeoutError) as exc:
                if attempts > self.budget.retries:
                    entry.update(status="error", error_type="transient",
                                 error=f"{tool}: {attempts} denemede başarısız ({exc or 'zaman aşımı'})")
                    output = None
                    break
                logger.warning("tool_retry agent=%s tool=%s attempt=%s", agent, tool, attempts)
            except (ValidationError, InputValidationError, QueryLimitError, ValueError) as exc:
                entry.update(status="error", error_type="invalid_args", error=f"Geçersiz argüman: {exc}"[:600])
                output = None
                break
            except (ToolError, DataSourceError, Exception) as exc:  # kalıcı hata: diğer agent'ları etkilemez
                entry.update(status="error", error_type="tool", error=f"{type(exc).__name__}: {exc}"[:600])
                output = None
                break
        entry.update(attempts=attempts, duration_ms=round((time.perf_counter() - started) * 1000, 1))
        ref = self.store.record(entry)
        self.emit({"kind": "tool", "node": agent, "round": round_no, "ref": ref,
                   **{k: v for k, v in entry.items() if k not in ("output", "agent")}})
        return output, ref
