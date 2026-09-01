# Phase 7 (v0.8.0) — Vision intake

Status: **approved** 2026-09-01.
Prior art: `2026-08-18-ley-khaa-design.md` §5.2, §11; `2026-08-30-phase-6-channel-adapters-design.md`.

---

## 1. Goal

Close the §11 line that is still open:

> Intake accepts **multi-modal** input; a request carrying a **pasted image** is understood via
> vision, its extraction **frozen as a reproducible checkpoint**.

Today `Stage.VISION_EXTRACTION` exists in `llm/router.py` (Opus at both complexities, 8000 max
tokens) and **nothing calls it**. Images reach the interpreter as filenames, and
`executor/resolver.py`'s `_TEXTUAL` set excludes them from ever becoming a script input — its
comment says so explicitly: *"An IMAGE attachment needs vision extraction, which is not built in
this phase."* That comment becomes false in this phase and Task 1 of the plan must correct it.

## 2. Locked decisions

1. **One extraction, two consumers.** An image is extracted once; the interpreter reads the summary
   so the request is *understood*, and the resolver can bind the content as a *data* input. Not two
   mechanisms.
2. **Lazy, at the point of need** — §5.2's own words. Not at intake: that would contradict
   "images are not interpreted here" and spend an Opus call on images in messages the relevance
   filter discards.
3. **The cache row is the checkpoint.** Keyed on the SHA-256 of the image bytes.
4. **Bytes are fetched by us, for both platforms** — see §3.3 for why the API's URL source form is
   rejected.
5. **Degrade, never block.** No API key, or a failed extraction, leaves the task running with the
   image named but unread, and the manifest says so. The zero-account demo is a hard invariant.
6. **No new task states.** Extraction is called from inside `TaskDriver.advance()`, which already
   owns the automatic path.

## 3. Design

### 3.1 The extraction contract

```python
class VisionExtraction(BaseModel):
    kind: Literal["table", "text"]
    content: str   # CSV when kind == "table"; prose otherwise
    summary: str   # one line, for the interpreter's prompt
```

Two fields rather than one because the consumers want different things and have different budgets:
the interpreter needs a sentence it can afford inside a prompt, the resolver needs the whole CSV as
bytes to compute on. Deriving one from the other by truncation would give the interpreter a
half-row of CSV, which is worse than a sentence.

`kind` decides the checkpoint's extension: `.csv` for a table, `.txt` for prose. It is a closed
`Literal`, so a model returning anything else fails structured-output validation rather than
producing a file with a lying extension.

### 3.2 `VisionExtractor` and the cache

```python
class VisionExtractor:
    def extract(self, *, image: bytes, media_type: str, filename: str) -> ExtractionRecord
```

**It always returns a record, never `None`.** On a hit it returns the stored row without a model
call; on a miss it calls the LLM through `Stage.VISION_EXTRACTION` and writes the row; when no
extraction was possible it returns the unread record of §3.6. One return shape means no consumer
has to branch on `None`, and the "was it actually read?" question has exactly one expression:
**`content` is empty**. The resolver binds only records with non-empty content; the interpreter
prints whatever `summary` says either way.

**Keyed on `sha256(image_bytes)`**, not on message id or attachment index. Three reasons: the same
screenshot pasted in two messages costs one Opus call; the hash is a content identity that survives
re-drives, repairs and escalate/answer loops; and it is the natural thing for a manifest to attest.

This is the third instance of the cache-behind-a-seam shape (`RegistryMatcher`, `MemoryMatcher`),
and it must be proven the same way Phase 4 proved those: a call counter wrapped around the **real**
offline client, asserting the second extraction makes zero calls.

### 3.3 `ImageFetcher` and its security boundary

Channel attachments carry a **URL**, not bytes — Phase 6 stopped deliberately at
`"content": str(item.get("url_private") or "")`. Resolving one means handing the Slack bot token to
an HTTP client, so the boundary is explicit:

- **https only**, and the host must be on an allowlist (`files.slack.com`, `cdn.discordapp.com`,
  `media.discordapp.net`), configurable via `LEY_KHAA_IMAGE_HOSTS`.
- **The Slack token is attached only to Slack hosts.** A URL that is allowlisted but not Slack gets
  no `Authorization` header. This is the rule that stops a payload-supplied URL exfiltrating the
  token.
- **Redirects are not followed.** A 302 to an off-allowlist host would otherwise defeat both rules
  above.
- A byte cap (`LEY_KHAA_IMAGE_MAX_BYTES`, default 5 MB) enforced on the response body as it is read,
  not on `Content-Length`, which is attacker-controlled.
- A timeout, and the response `Content-Type` must actually be an image.

**Why not let the API fetch it.** The Messages API accepts
`{"type": "image", "source": {"type": "url", "url": ...}}`, which would remove this component for
Discord entirely. Rejected: Discord CDN URLs carry expiring signed query parameters
(`?ex=&is=&hm=`), so the URL is not a stable identity, and if we never hold the bytes we cannot
compute `image_sha256` — which is the whole basis of the frozen checkpoint. Slack's `url_private`
is not publicly fetchable at all. Fetching both keeps one code path and one identity.

### 3.4 The `LLMClient` extension

`LLMClient` today is exactly one method plus a `name`:

```python
def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T
```

It gains one more, and every implementation — `AnthropicLLM`, `HeuristicLLM`, `FakeLLM` — must
satisfy it:

```python
def extract_image(
    self, *, choice: ModelChoice, system: str, user: str,
    image: bytes, media_type: str, output_format: type[T],
) -> T
```

`AnthropicLLM.extract_image` is `parse` with an image content block ahead of the text, base64
encoded, and keeps the existing `supports_thinking` gate untouched:

```python
"messages": [{"role": "user", "content": [
    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
    {"type": "text", "text": user},
]}]
```

`name` keeps its existing contract — the manifest records **who actually produced the extraction**,
not the model the router would have picked. That discipline was established by Phase 3's follow-ups
and is why an offline run must never credit `claude-opus-5`.

### 3.5 The two consumers

**Interpreter.** `interpreter/interpreter.py::_render` currently emits
`attachment: {kind} named {name}`. For an image with an extraction it emits the `summary` as well,
which is the whole of "understood via vision". Unchanged for every other attachment kind.

**Resolver.** `executor/resolver.py::_TEXTUAL` currently excludes `IMAGE`. An image **that has an
extraction** becomes bindable: `_attachments_for` includes it, name matching uses the filename stem
exactly as it does today, and `ResolvedInput.source` — currently `"attachment" | "catalog"` — gains
`"vision"` so the manifest distinguishes bytes a human supplied from bytes a model read.

An image with no extraction stays excluded, which keeps the offline path identical to today's.

### 3.6 Degradation

There is one rule: **an unreadable image never blocks a task.**

- No vision backend (`HeuristicLLM`) → a deterministic record: `kind="text"`, empty `content`,
  `summary` naming the file and stating it was not read, `model="heuristic"`.
- Fetch failure, oversize, disallowed host, or an API error → the same shape, with the reason in
  `summary`, plus a **dead letter** so the drop is visible (§3.8 of Phase 6). Kind is `inbound`,
  not a new fourth kind: the existing three are `inbound | outbound | connection`, and this is a
  failure to take in something that arrived, which is what `inbound` already means. The alternative
  — a `vision` kind — would make the dashboard's panel enumerate implementation stages rather than
  directions of failure.
- Either way the task proceeds on the text it has. If that leaves the spec short, the existing
  clarification loop asks — which is the correct behaviour and needs no new code.

This is deliberately *not* the "park for clarification" option: a no-API-key demo must complete.

## 4. Data model

One new table, one new migration `0008_vision` (head is `0007_channels`):

| column | type | note |
|---|---|---|
| `image_sha256` | str, PK | identity of the bytes |
| `kind` | str | `table` \| `text` |
| `content` | text | CSV or prose |
| `summary` | text | one line |
| `media_type` | str | as fetched |
| `byte_size` | int | what was read |
| `model` | str | `LLMClient.name` — who actually did it |
| `created_at` | datetime | |

Text, not JSON, for `content` — the same reasoning Phase 6 recorded for `dead_letters.payload`:
Postgres's `json` type has no equality operator, and this column is written once and only ever read.

`server_default=text("''")` on every non-null string column, or the drift guard fails — the trap
Phase 5 documented.

## 5. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEY_KHAA_VISION` | `on` | `off` disables extraction entirely (degrades per §3.6) |
| `LEY_KHAA_IMAGE_HOSTS` | the three above | comma-separated fetch allowlist |
| `LEY_KHAA_IMAGE_MAX_BYTES` | `5242880` | hard cap on a fetched body |

## 6. Testing

- **Pure**: the extraction contract, `kind`→extension mapping, hashing, stem matching for a
  vision-sourced input.
- **The fetcher, table-driven**: disallowed host, http scheme, redirect, oversize body, wrong
  content-type, and — asserted on the outgoing headers — **the Slack token is never sent to a
  non-Slack host**.
- **The cache**: a counter around the real offline client; the second extraction makes zero calls,
  re-asserted inside the test so it cannot degrade into "two runs happened to agree".
- **Offline end-to-end**: an image attachment flows intake → interpreter → executor with the
  stand-in; the task completes and the manifest records `model: "heuristic"`.
- **Bundle**: the checkpoint lands in `inputs/` and the manifest attests hash, model and
  `source: "vision"`.

Every new assertion follows the phase-4 rule: delete the behaviour it guards, watch it fail for the
right reason, restore.

## 7. Definition of done

- A message carrying an image produces a task whose spec was informed by the image's content.
- The extraction is written once and reused: a second drive makes no model call.
- The bundle contains `inputs/extracted_<stem>.{csv,txt}` and the manifest attests its hash, the
  producing model, and `source: "vision"`.
- With no `ANTHROPIC_API_KEY`: the suite is green, `docker compose up` still demos, and an image is
  carried-not-read with the manifest saying so.
- The Slack token is provably never sent off a Slack host.
- Full suite green, 0 skipped, 0 warnings, typecheck clean.

## 8. Known limits, stated up front

- **Ollama (v0.9.0) is text-only**, so once it lands vision will not work on the offline path. A
  local vision-capable model is roadmap (§2 of the design spec), and this must be said plainly in
  the README rather than discovered.
- **No re-extraction.** If vision misreads a table, the frozen checkpoint stays wrong until the row
  is deleted. Freezing is the point — it is what makes a re-run reproducible — so a manual override
  is a deliberate follow-up, not an omission.
- **Images are not stored**, only their extraction. Re-reading an image whose URL has expired is
  therefore impossible; the checkpoint is the durable artifact.
- One extraction per image regardless of what a later step wanted from it: a screenshot read as
  prose is not re-read as a table.

## 9. Out of scope

Media *generation* (roadmap), a local vision model (roadmap), PDF or document input, and
re-extraction UI.
