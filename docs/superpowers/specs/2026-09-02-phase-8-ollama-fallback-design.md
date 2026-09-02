# Phase 8 (v0.9.0) — Ollama offline fallback

Status: **approved** 2026-09-02.
Prior art: `2026-08-18-ley-khaa-design.md` §5.13 (Model Router), §7, §11; `2026-09-01-phase-7-vision-intake-design.md`.

---

## 1. Goal

Close the last open §11 line:

> **Ollama offline fallback (Sonnet slot available).**

Today a clone without `ANTHROPIC_API_KEY` runs on `HeuristicLLM` — a regex stand-in that does
keyword matching, with no language understanding and no reasoning about which messages belong
together. It exists so a fresh clone demos, not so it works.

This phase adds a third implementation of the existing `LLMClient` protocol that talks to a local
Ollama daemon, so the keyless path gets a **real model** instead of a stub. Nothing about the
Anthropic path changes.

---

## 2. Locked decisions

1. **Static backend only.** `LEY_KHAA_LLM=ollama` selects the backend once, at startup. The spec's
   §7 runtime step-down ("LLM call failure → retry, then fall back to local Ollama") is **out of
   scope**, and this phase FILES it as a new backlog item (it does not exist yet). Reason: a per-call fallback makes the producer a property of
   the *call*, not the client, which breaks the `LLMClient.name` contract the manifest depends on.
   That is a design change deserving its own phase.
2. **One local model for every stage.** `LEY_KHAA_OLLAMA_MODEL` names it. The router's
   `choice.model` (a Claude id) is **ignored**; `choice.max_tokens` is honoured. Reason: most people
   running Ollama have exactly one model pulled, and requiring two is friction on the very path this
   phase exists to serve.
3. **Unreachable Ollama falls back to the heuristic, loudly.** One-time WARNING naming the real
   cause, then `HeuristicLLM`. Reason: it matches the precedent already in `build_llm`, and
   `docker compose up` must keep demoing.
4. **Vision stays text-only**, as §11 already states. An Ollama run cannot read an image.
5. **`name` records the real model** — `ollama:<model>`, never a Claude id, never `"anthropic"`.

---

## 3. Design

### 3.1 `OllamaLLM` — the third implementation

New file: `backend/ley_khaa/llm/ollama.py`. It implements the **existing** `LLMClient` protocol
unchanged. `AnthropicLLM` and `HeuristicLLM` are not touched.

The shape that makes this cheap: `HeuristicLLM.parse` dispatches per output type
(`RelevanceVerdict`, `CrystallizerOutput`, `TaskSpec`, `SynthesizedScript`, `RegistryDecision`,
`MemoryDecision`, `ProjectChoice`, …), each with hand-written regex. `OllamaLLM.parse` needs none of
that: Ollama accepts a **JSON schema** as its `format` parameter, and every output type in this
codebase is a Pydantic model that can produce one.

```python
resp = self._client.chat(
    model=self.model,
    messages=[{"role": "system", "content": system},
              {"role": "user", "content": user}],
    format=output_format.model_json_schema(),
    options={"num_predict": choice.max_tokens},
)
return output_format.model_validate_json(resp.message.content)
```

One implementation covers every stage that exists today and every one added later.

**What is carried over from the router and what is not:**

| `ModelChoice` field | Treatment |
|---|---|
| `model` | **Ignored.** It names a Claude model; the local model comes from config (decision 2). |
| `max_tokens` | **Honoured**, as `options.num_predict`. Synthesis still gets its 16000, vision extraction its 8000. |
| `supports_thinking` | **Unused.** It exists to keep the `thinking` parameter away from pre-4.6 Anthropic models. Ollama has its own separate `think` parameter and never receives Anthropic's. |

### 3.2 Provenance

`name` is `f"ollama:{self.model}"` — e.g. `ollama:qwen2.5`.

This is the same contract as `SandboxRunner.name` and the Phase 7 `extracted_by` field: **the
manifest records what actually produced the artifact.** An Ollama run must never be creditable to
`claude-opus-5`, and the model string must name the specific local model, because "ollama" alone
does not identify what produced a script.

### 3.3 Vision is text-only, and needs no new machinery

`OllamaLLM.extract_image` returns an empty `VisionExtraction`, exactly as `HeuristicLLM` does.

Phase 7 already made this safe by construction: `VisionExtractor` turns an empty extraction into a
**stored carried-not-read record**, and the manifest's `images` block names the unread image. So
§7's requirement — "image steps error out gracefully rather than guess" — is satisfied with no new
degradation path, no new error type, and no change to the extractor.

One interaction worth naming: the Phase 7 cache re-extracts when the stored `model` differs from the
current client's name. A record written by `ollama:qwen2.5` is therefore retried if the operator
later configures Anthropic — which is the correct behaviour, and falls out of the existing rule.

### 3.4 Selection, probing, and failure

`build_llm` gains an `ollama` branch. Before returning the client it probes **once**, at startup:

1. **Is the daemon reachable?** A cheap `client.list()` with a short timeout.
2. **Is the configured model actually pulled?** Check it appears in that listing.

Distinguishing these two matters for the user: a missing model should say
`run: ollama pull <model>`, not fail cryptically on the first request half a minute later.

Any failure → one-time WARNING naming the real cause → `HeuristicLLM`. The existing
`_warned_about_fallback` pattern applies: `build_llm` runs per request and per background sweep, so
the notice is said once, not every few seconds.

The warning must name the cause specifically. "Falling back" alone is what leads a reader to believe
they are looking at model output when they are looking at regex output.

### 3.5 Docker

`docker-compose.yml` has no `extra_hosts`, so a container **cannot** reach a host-run Ollama through
`localhost`. The compose service gains a `host.docker.internal` mapping, and
`LEY_KHAA_OLLAMA_HOST` defaults to `http://localhost:11434` for direct runs while compose passes the
host-gateway form. Documented either way: it is the difference between "works" and "silently falls
back to regex" for anyone who tries Ollama through compose.

---

## 4. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEY_KHAA_LLM` | `anthropic` | Existing. Gains `ollama` as a third accepted value. |
| `LEY_KHAA_OLLAMA_MODEL` | `qwen2.5` | The local model, used for every stage. |
| `LEY_KHAA_OLLAMA_HOST` | `http://localhost:11434` | Where the daemon lives. |

**Every one of these must be read falsy-safe** — `os.getenv(NAME) or default`, never
`os.getenv(NAME, default)`. Compose passes `${VAR:-}`, which *sets* the variable to an empty string
rather than leaving it unset, so the two-argument form returns `""` and the default never fires.
That exact bug crashed the backend at import in Phase 7 and must not be repeated.

`ollama` is added to `pyproject.toml` as a **pinned** dependency, in the style of
`requests==2.34.2` and `slack_sdk==3.44.0`.

---

## 5. Testing

- `OllamaLLM(client=...)` accepts an injected double, mirroring `AnthropicLLM(client=...)`. The real
  client is constructed only when none is passed.
- **No test may reach `localhost:11434`.** `conftest` already pins `LEY_KHAA_LLM=heuristic`, so
  nothing in the existing suite constructs this client.
- Coverage: the JSON schema is passed as `format`; `num_predict` carries `choice.max_tokens`;
  `name` is `ollama:<model>`; `choice.model` is genuinely ignored; `extract_image` returns an empty
  extraction and makes no call; and **both startup fallback paths** — unreachable daemon, and a model
  that is not pulled — warn with the cause named and return `HeuristicLLM`.
- A **malformed model response is not a fallback path.** The backend is chosen once at startup
  (decision 1), so a response that fails Pydantic validation is a failed *call*, raised and handled
  by the same machinery that already handles a failed Anthropic call. Test it as a raise, not as a
  silent downgrade — a per-call swap would reintroduce exactly the producer-identity problem that
  put runtime step-down out of scope.
- Every guard gets a **mutation** proving a named test fails without it. A guard that is merely
  asserted is not pinned — the recurring defect of the last two phases.

---

## 6. Definition of done

- `LEY_KHAA_LLM=ollama` with a running daemon drives a request end to end on a local model.
- The bundle manifest names `ollama:<model>` as the producer, never a Claude id.
- An image on the Ollama path is carried-not-read, with the manifest saying so.
- With Ollama unreachable or the model unpulled: a WARNING naming the cause, the heuristic takes
  over, `name` is `heuristic`, and `docker compose up` still demos.
- The Anthropic path is byte-for-byte unchanged.
- Full suite green, 0 skipped, 0 warnings.

---

## 7. Known limits, stated up front

- **No image understanding offline.** Vision requires `ANTHROPIC_API_KEY`. A local vision model is
  roadmap, not v1 (backlog 21).
- **Output quality depends entirely on the local model.** A small quantised model will produce
  weaker specs and scripts than Opus. The system does not detect this or warn about it beyond
  naming the model in the manifest.
- **No runtime step-down.** If Ollama fails mid-run it is not replaced by Claude, or vice versa. The
  backend is chosen once, at startup (decision 1). Filed as a new backlog item by this phase.
- **Schema adherence is enforced by Ollama, not by us.** Models vary in how well they honour a JSON
  schema; a response that fails Pydantic validation is a failed call, handled like any other.

---

## 8. Out of scope

- Runtime step-down / retry-then-fallback (spec §7) — filed as a new backlog item by this phase.
- Per-stage or two-tier local model mapping — decision 2.
- A local vision model — backlog 21.
- Any change to the `LLMClient` protocol, `AnthropicLLM`, or `HeuristicLLM`.
