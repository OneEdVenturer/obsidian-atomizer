"""LLM client abstraction: Anthropic API or local OpenAI-compatible endpoint.

Every call logs input/output token usage and accumulates totals for cost
reporting. Determinism is requested via temperature=0 where the target model
supports sampling parameters (newer Opus-tier models reject them and run
deterministically-greedy by design of this tool's prompts).
"""

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger("atomizer.llm")

# Models that reject sampling parameters (temperature/top_p/top_k return 400).
_NO_SAMPLING_MARKERS = ("opus-4-7", "opus-4-8", "fable")


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


@dataclass
class UsageTracker:
    """Accumulates token usage across all calls in a run."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    _pricing: dict = field(default_factory=dict)

    def add(self, response: LLMResponse) -> None:
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.calls += 1

    def estimated_cost(self, model: str) -> float | None:
        """Estimated USD cost of the run total. None if model is unpriced."""
        return self.cost_for(model, self.input_tokens, self.output_tokens)

    def cost_for(self, model: str, input_tokens: int,
                 output_tokens: int) -> float | None:
        """Estimated USD cost for an arbitrary token count (e.g. one file).

        Pricing values are USD per 1M tokens. Returns None if the model has
        no pricing entry.
        """
        price = self._pricing.get(model)
        if not price:
            return None
        return (
            input_tokens / 1_000_000 * float(price.get("input", 0))
            + output_tokens / 1_000_000 * float(price.get("output", 0))
        )


class LLMClient:
    """Base interface. Subclasses implement _complete()."""

    def __init__(self, model: str, max_tokens: int, usage: UsageTracker):
        self.model = model
        self.max_tokens = max_tokens
        self.usage = usage

    def complete(self, system: str, user_content: str) -> LLMResponse:
        response = self._complete(system, user_content)
        self.usage.add(response)
        log.info(
            "LLM call complete (model=%s): %d input tokens, %d output tokens",
            self.model,
            response.input_tokens,
            response.output_tokens,
        )
        return response

    def _complete(self, system: str, user_content: str) -> LLMResponse:
        raise NotImplementedError


class AnthropicClient(LLMClient):
    """Calls the Anthropic Messages API via the official SDK (streaming)."""

    def __init__(self, model: str, max_tokens: int, usage: UsageTracker,
                 api_key_env: str = "ANTHROPIC_API_KEY"):
        super().__init__(model, max_tokens, usage)
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The 'anthropic' package is required for provider=anthropic. "
                "Install with: pip install anthropic"
            ) from exc
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Environment variable {api_key_env} is not set. "
                f"Set it to your Anthropic API key."
            )
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key)

    def _supports_sampling(self) -> bool:
        model_lower = self.model.lower()
        return not any(marker in model_lower for marker in _NO_SAMPLING_MARKERS)

    def _complete(self, system: str, user_content: str) -> LLMResponse:
        kwargs = {}
        if self._supports_sampling():
            kwargs["temperature"] = 0.0  # deterministic extraction

        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                **kwargs,
            ) as stream:
                message = stream.get_final_message()
        except self._anthropic.AuthenticationError as exc:
            raise RuntimeError("Anthropic API key is invalid.") from exc
        except self._anthropic.NotFoundError as exc:
            raise RuntimeError(
                f"Model '{self.model}' not found — check the model ID in "
                f"config.yaml (it may be retired)."
            ) from exc
        except self._anthropic.APIError as exc:
            raise RuntimeError(f"Anthropic API error: {exc}") from exc

        text = "".join(b.text for b in message.content if b.type == "text")
        if message.stop_reason == "max_tokens":
            log.warning(
                "Response hit the max_tokens limit (%d) — output may be "
                "truncated. Increase max_output_tokens in config.yaml.",
                self.max_tokens,
            )
        usage = message.usage
        input_tokens = usage.input_tokens + (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        return LLMResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
        )


class LocalClient(LLMClient):
    """Calls a local OpenAI-compatible /chat/completions endpoint.

    Works with LM Studio, Ollama (OpenAI mode), llama.cpp server, vLLM, etc.
    Uses stdlib urllib so no extra dependency is required.
    """

    def __init__(self, model: str, max_tokens: int, usage: UsageTracker,
                 endpoint: str, timeout: int = 600):
        super().__init__(model, max_tokens, usage)
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _complete(self, system: str, user_content: str) -> LLMResponse:
        url = f"{self.endpoint}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach local LLM endpoint {url}: {exc}. "
                f"Is the local server running?"
            ) from exc

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected response shape from local endpoint: "
                f"{json.dumps(body)[:500]}"
            ) from exc

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        if not usage:
            log.warning(
                "Local endpoint returned no usage data; token counts for "
                "this call are recorded as 0."
            )
        return LLMResponse(text=text, input_tokens=input_tokens,
                           output_tokens=output_tokens)


def build_client(config: dict, provider_override: str | None = None,
                 model_override: str | None = None) -> LLMClient:
    """Construct the right client from config.yaml plus CLI overrides."""
    provider = (provider_override or config.get("provider", "anthropic")).lower()
    max_tokens = int(config.get("max_output_tokens", 16000))
    usage = UsageTracker(_pricing=config.get("pricing", {}) or {})

    if provider == "anthropic":
        model = model_override or config.get("model", "claude-sonnet-4-6")
        client = AnthropicClient(
            model=model,
            max_tokens=max_tokens,
            usage=usage,
            api_key_env=config.get("api_key_env", "ANTHROPIC_API_KEY"),
        )
    elif provider == "local":
        model = model_override or config.get("local_model", "local-model")
        client = LocalClient(
            model=model,
            max_tokens=max_tokens,
            usage=usage,
            endpoint=config.get("local_endpoint", "http://localhost:1234/v1"),
            timeout=int(config.get("local_timeout_seconds", 600)),
        )
    else:
        raise RuntimeError(
            f"Unknown provider '{provider}' — expected 'anthropic' or 'local'."
        )

    log.info("LLM provider: %s, model: %s", provider, client.model)
    return client
