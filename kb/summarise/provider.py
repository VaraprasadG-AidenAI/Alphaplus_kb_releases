"""
Talking to a model, over plain HTTPS, with no SDK.

Four providers plus 'none'.  They exist because we do not know yet what the
client will permit, and that decision should be a setting rather than a
rewrite:

    anthropic   Claude
    openai      GPT
    deepseek    cheapest of the three, same wire format as OpenAI
    local       anything OpenAI-shaped inside the client's own network -
                Ollama, vLLM, LM Studio, a corporate gateway
    none        no model at all; pages are built from facts alone

`urllib` rather than `requests` or an SDK, for the same reason the MCP server
has no dependencies: this eventually runs somewhere `pip install` is a change
request.

Nothing here retries forever.  A failed member is reported and skipped, never
silently written as an empty page.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

from .. import config


class ProviderError(RuntimeError):
    pass


#: Reasoning models wrap their working in these before answering.
THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)


def _visible(message: Dict[str, object]) -> str:
    """The answer, with any chain of thought removed.

    A reasoning model spends its token budget thinking and only then answers.
    Ollama returns that working in a separate `reasoning` field and leaves
    `content` empty; other servers inline it in <think> tags. Either way it is
    not the summary, and writing it to a page would be worse than writing
    nothing.

    If a model returns thinking and no answer it ran out of budget before it
    got to the point. Say so - that is a fixable configuration problem, not a
    mysteriously blank page.
    """
    content = str(message.get("content") or "")
    content = THINK.sub("", content).strip()
    if content:
        return content
    if message.get("reasoning"):
        raise ProviderError(
            "the model returned reasoning but no answer - it used its whole "
            "token budget thinking. Use a non-reasoning model, or raise the "
            "budget.")
    return ""


class Provider:
    """One model endpoint, and a running count of what it cost."""

    def __init__(self, settings: Optional[Dict[str, str]] = None) -> None:
        self.settings = settings or config.llm_settings()
        self.name = self.settings.get("name", "none")
        self.dialect = self.settings.get("dialect", "none")
        self.model = self.settings.get("model", "")
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self.failures = 0

    # -------------------------------------------------------------------
    @property
    def usable(self) -> bool:
        return self.dialect != "none" and bool(self.settings.get("key")
                                               or self.name == "local")

    def why_not(self) -> str:
        if self.dialect == "none":
            return ("no model configured - set ALPHAPLUS_LLM to anthropic, "
                    "openai, deepseek or local")
        if not self.settings.get("key") and self.name != "local":
            return f"no API key in {self.settings.get('key_env')}"
        return ""

    # -------------------------------------------------------------------
    def _request(self, system: str, user: str,
                 max_tokens: int) -> Tuple[str, int, int]:
        url = self.settings["url"]
        key = self.settings.get("key", "")
        headers = {"content-type": "application/json"}

        if self.dialect == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            body = {"model": self.model, "max_tokens": max_tokens,
                    "system": system,
                    "messages": [{"role": "user", "content": user}]}
        else:                                    # openai, deepseek, local
            if key:
                headers["authorization"] = f"Bearer {key}"
            body = {"model": self.model, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
            if self.name == "local":
                # Top level, not inside an `options` block. This endpoint is
                # Ollama's OpenAI-compatible shim and it ignores `options`
                # entirely - which was tested, after an `options` block spent a
                # run doing nothing at all.
                #
                # Greedy decoding, because two runs over one menu produced
                # "back-office maintenance and audit functions" once and
                # "back-end maintenance ... data extraction, file building" the
                # next, from identical facts. A page nobody can reproduce
                # cannot be reviewed, and a description of a codebase that
                # changes when you ask again is not a description.
                body["temperature"] = 0
                body["seed"] = 20260826

                # `num_ctx` is NOT settable here - it is not an OpenAI
                # parameter and the shim drops it. So the context is whatever
                # the model was loaded with, and a prompt longer than that is
                # silently truncated: no error, no warning, no marker in the
                # reply, just a summary of whatever survived. That is the worst
                # failure available to a tool whose whole purpose is not making
                # things up.
                #
                # Since it cannot be set, it is checked. Refusing to send a
                # prompt that will not fit is worth more than a parameter that
                # looks like it is doing something.
                estimate = (len(system) + len(user)) // 4 + max_tokens
                if estimate > config.LLM_CONTEXT:
                    raise ProviderError(
                        f"prompt is about {estimate:,} tokens and the context "
                        f"is {config.LLM_CONTEXT:,}. This endpoint cannot set "
                        f"num_ctx, so the server would truncate it silently. "
                        f"Load the model with a larger context, or set "
                        f"ALPHAPLUS_LLM_CONTEXT if it already has one.")

        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(request,
                                    timeout=config.LLM_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))

        if self.dialect == "anthropic":
            text = "".join(block.get("text", "")
                           for block in payload.get("content", []))
            usage = payload.get("usage", {})
            return (text, usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0))

        choices = payload.get("choices") or []
        message = choices[0]["message"] if choices else {}
        text = _visible(message)
        usage = payload.get("usage", {})
        return (text, usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0))

    # -------------------------------------------------------------------
    def complete(self, system: str, user: str, max_tokens: int = 900,
                 attempts: int = 3) -> str:
        """One completion.  Raises ProviderError rather than returning junk."""
        if not self.usable:
            raise ProviderError(self.why_not())

        last = ""
        for attempt in range(attempts):
            try:
                text, sent, got = self._request(system, user, max_tokens)
                self.calls += 1
                self.input_tokens += sent
                self.output_tokens += got
                if text.strip():
                    return text.strip()
                last = "the model returned nothing"
            except urllib.error.HTTPError as exc:
                last = self._explain(exc)
                # 4xx other than 429 will not improve by asking again
                if exc.code not in (408, 409, 429) and exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = self._explain_connection(exc)
                if self.name == "local":
                    break                       # the server is down, not busy
            except ProviderError as exc:        # terminal, do not retry
                last = str(exc)
                break
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                last = f"unexpected response shape: {exc}"
                break
            if attempt < attempts - 1:
                time.sleep(2 ** attempt)

        self.failures += 1
        raise ProviderError(last)

    # -------------------------------------------------------------------
    def _explain(self, exc: urllib.error.HTTPError) -> str:
        """Turn an HTTP failure into something worth acting on.

        The two that actually happen are a model that was never pulled and a
        key that is wrong. Both are one command away from fixed, so say which
        command rather than printing the raw body.
        """
        body = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 404 and self.name == "local":
            return (f"{self.model!r} is not pulled on this Ollama server.\n"
                    f"       Fix:  ollama pull {self.model}\n"
                    f"       Or:   ollama list   to see what you do have")
        if exc.code in (401, 403):
            return (f"HTTP {exc.code} - the key in "
                    f"{self.settings.get('key_env')} was rejected")
        if exc.code == 429:
            return "HTTP 429 - rate limited, backing off"
        return f"HTTP {exc.code}: {body}"

    def _explain_connection(self, exc: Exception) -> str:
        if self.name == "local":
            return (f"cannot reach {self.settings.get('url')} - "
                    f"{type(exc).__name__}.\n"
                    f"       Is the server running?  ollama serve")
        return f"{type(exc).__name__}: {exc}"

    # -------------------------------------------------------------------
    def cost_line(self) -> str:
        return (f"{self.calls:,} call(s), {self.input_tokens:,} in / "
                f"{self.output_tokens:,} out"
                + (f", {self.failures:,} failed" if self.failures else ""))


def check() -> int:
    """`python -m kb.summarise.provider` - does the configured model answer?"""
    provider = Provider()
    print(f"  provider   {provider.name}")
    print(f"  model      {provider.model or '-'}")
    print(f"  endpoint   {provider.settings.get('url') or '-'}")
    print(f"  key        {'present' if provider.settings.get('key') else 'absent'}")
    print(f"  summaries  {'on' if config.ENABLE_SUMMARIES else 'off'}")
    print(f"  send source{'  yes' if config.LLM_SEND_SOURCE else '  no'}")
    print()
    if not provider.usable:
        print(f"  not usable: {provider.why_not()}")
        print("  The knowledge base still works - facts, graph, search and")
        print("  lexicon need no model at all.")
        return 0
    try:
        reply = provider.complete("Answer in one word.", "Say OK.",
                                  max_tokens=16)
        print(f"  reply      {reply[:60]}")
        print(f"  usage      {provider.cost_line()}")
        print("\n  the model answers")
        return 0
    except ProviderError as exc:
        print(f"  FAILED     {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(check())
