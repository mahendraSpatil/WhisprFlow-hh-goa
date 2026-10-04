"""The one place CodeLoop talks to Claude, shared by the agents that use it."""

from __future__ import annotations

import os
from typing import Any

import anthropic

from app.orchestrator.events import StageLogger

DEFAULT_MODEL = "claude-opus-5-5"
# Claude API only: when Anthropic's safety classifiers decline a request (security work can
# trigger them), the API re-runs it on a fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT_S = 120.0


def llm_enabled() -> bool:
    """CODELOOP_LLM=off keeps repository code from ever being sent to the API."""
    return os.environ.get("CODELOOP_LLM", "on").lower() not in ("off", "0", "false")


class ClaudeClient:
    """Messages API calls with CodeLoop's error policy.

    Credentials come from the environment as for any Anthropic SDK client. A missing or rejected
    credential disables the client for the rest of the run after one warning; other API errors
    only fail the request that hit them. ``create`` returns None whenever there is no response.
    """

    def __init__(
        self, log: StageLogger, client: anthropic.AsyncAnthropic | None = None, model: str | None = None
    ) -> None:
        self._log = log
        self._client = client or anthropic.AsyncAnthropic(timeout=REQUEST_TIMEOUT_S)
        self.model = model or os.environ.get("CODELOOP_LLM_MODEL", DEFAULT_MODEL)
        self.disabled = False

    async def create(
        self, *, system: str, messages: list[dict[str, Any]], effort: str, max_tokens: int, what: str = "request"
    ) -> Any | None:
        if self.disabled:
            return None
        try:
            return await self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                output_config={"effort": effort},
                system=system,
                messages=messages,
            )
        except TypeError as exc:
            if "authentication" not in str(exc).lower():
                raise
            self._disable("no Anthropic credentials found (set ANTHROPIC_API_KEY)")
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            self._disable(f"Anthropic rejected the credentials ({exc.status_code})")
        except anthropic.APIError as exc:
            self._log.warning(f"Claude request failed for {what}: {type(exc).__name__}")
        return None

    def refusal(self, response: Any, what: str) -> bool:
        """True when the safety classifiers declined even after the server-side fallback."""
        if response.stop_reason != "refusal":
            return False
        category = getattr(response.stop_details, "category", None)
        self._log.warning(f"Claude declined {what}" + (f" ({category})" if category else ""))
        return True

    @staticmethod
    def text(response: Any) -> str:
        return "".join(block.text for block in response.content if block.type == "text").strip()

    def _disable(self, reason: str) -> None:
        if self.disabled:
            return  # concurrent requests all hit the same failure; say it once
        self.disabled = True
        self._log.warning(f"Claude disabled for this run: {reason}")

    async def aclose(self) -> None:
        await self._client.close()
