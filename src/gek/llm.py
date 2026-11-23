"""Model calls through litellm, logged to death.

Custom OpenAI-compatible providers (like vllm-sr) work with model names of
the form "openai/<model>" plus api_base/api_key from .env.
"""

from __future__ import annotations

import time
from typing import Any

from .config import Config
from .logs import RunLog


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg: Config, log: RunLog):
        self.cfg = cfg
        self.log = log
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_cost = 0.0
        self.calls = 0
        self._next_budget_note = 100_000
        # Tests replace this with a scripted fake: async (model, messages, tools, actor) -> dict
        self.fake = None

    @property
    def max_tokens_budget(self) -> int:
        return int(self.cfg.section("budget").get("max_total_tokens") or 0)

    def check_budget(self) -> None:
        cap = self.max_tokens_budget
        if cap and self.total_tokens >= cap:
            raise BudgetExceeded(f"token budget spent: {self.total_tokens} >= {cap}")

    _resolved: dict[str, str] = {}

    def resolve_model(self, model: str) -> str:
        """litellm needs a known provider prefix. With a custom api_base, an unknown or
        missing prefix means an OpenAI-compatible server: prepend 'openai/' (litellm strips
        it again, so the server receives the name as written in config.json)."""
        if model in self._resolved:
            return self._resolved[model]
        import litellm

        litellm.suppress_debug_info = True  # no "Provider List" banners in the console
        import logging

        for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm", "httpx"):
            logging.getLogger(name).setLevel(logging.ERROR)
        out = model
        if self.cfg.api_kwargs().get("api_base"):
            try:
                litellm.get_llm_provider(model)
            except Exception:
                out = "openai/" + model
                self.log.event("note", "harness", model_rewritten={"from": model, "to": out},
                               why="unknown provider prefix with custom api_base; treating as OpenAI-compatible")
        self._resolved[model] = out
        return out

    async def chat(self, actor: str, model: str, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        """Returns the assistant message as a plain dict (role, content, tool_calls)."""
        self.check_budget()
        llm_cfg = self.cfg.section("llm")
        tool_names = [t["function"]["name"] for t in tools or []]
        self.log.event("llm_request", actor, model=model, n_messages=len(messages), tools=tool_names)
        t0 = time.monotonic()
        try:
            if self.fake is not None:
                msg, usage = await self.fake(model, messages, tools, actor)
                cost = 0.0
            else:
                import litellm

                model = self.resolve_model(model)
                kwargs: dict[str, Any] = dict(
                    model=model, messages=messages,
                    timeout=llm_cfg.get("timeout_s", 600),
                    num_retries=llm_cfg.get("num_retries", 3),
                    **self.cfg.api_kwargs(),
                    **(llm_cfg.get("extra_params") or {}),
                )
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if llm_cfg.get("temperature") is not None:
                    kwargs["temperature"] = llm_cfg["temperature"]
                resp = await litellm.acompletion(**kwargs)
                m = resp.choices[0].message
                msg = {"role": "assistant", "content": m.content or ""}
                if getattr(m, "tool_calls", None):
                    msg["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                        for tc in m.tool_calls
                    ]
                u = resp.usage
                usage = {"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                         "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                         "total_tokens": getattr(u, "total_tokens", 0) or 0}
                try:
                    cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
                except Exception:
                    cost = 0.0
                usage["served_model"] = getattr(resp, "model", None)
        except BudgetExceeded:
            raise
        except Exception as e:
            self.log.event("llm_error", actor, model=model, error=f"{type(e).__name__}: {e}",
                           ms=round((time.monotonic() - t0) * 1000))
            raise
        ms = round((time.monotonic() - t0) * 1000)
        self.calls += 1
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)
        self.total_cost += cost
        self.log.event("llm_response", actor, model=model, ms=ms, usage=usage, cost=cost,
                       content=msg.get("content", ""),
                       tool_calls=[{"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                                   for tc in msg.get("tool_calls", [])],
                       totals={"tokens": self.total_tokens, "cost": round(self.total_cost, 6),
                               "calls": self.calls})
        if self.total_tokens >= self._next_budget_note:
            self.log.event("budget", "harness", total_tokens=self.total_tokens,
                           cap=self.max_tokens_budget, cost=round(self.total_cost, 6))
            self._next_budget_note += 100_000
        return msg
