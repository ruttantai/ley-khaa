# Getting started

A first run of ley-khaa, from a fresh clone to a finished task with a downloadable deliverable.

Every step below was run on macOS (Apple Silicon, Docker via Colima). The walkthrough was first
recorded against `v0.6.0` and its commands, output and test counts are kept current with the
released tag — `v0.10.0` at the time of writing. Where a step produces output worth checking, the
real output is shown.

---

## 1. What you need

| | Version | Needed for |
|---|---|---|
| Docker | 26.1+ | The one-command run, and the sandbox that executes generated code |
| Python | 3.12 | The no-Docker dev loop |
| Node | 20 | The dashboard |

An `ANTHROPIC_API_KEY` is **optional to start** and **required to be useful** — see step 3.

All data in this project is synthetic. Nothing here connects to a real employer's Slack, data, or
credentials.

---

## 2. The fastest way to see it work

```bash
git clone https://github.com/ruttantai/ley-khaa.git
cd ley-khaa
docker compose up
```

That brings up Postgres, the backend, and the dashboard, then seeds a demo by replaying a synthetic
conversation through the real intake pipeline.

- Dashboard: <http://localhost:5173>
- API: <http://localhost:8000>

Check it is alive:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

---

## 3. Decide about the API key first

This matters more than anything else on this page.

**Without `ANTHROPIC_API_KEY`,** the app runs on `HeuristicLLM` — an offline regex stand-in. It
starts, seeds, executes, and produces real bundles, so it is genuinely useful for checking the
plumbing. But it does no language understanding at all. On startup you will see:

```
ANTHROPIC_API_KEY is not set — falling back to HeuristicLLM, the offline regex stand-in.
Relevance and crystallizer results will be crude: keyword matching only, no language
understanding, no reasoning about which messages belong together.
```

That warning is deliberate: silently degrading to a regex stub is how someone ends up believing
they are looking at model output.

**With the key set,** you get the actual product — real crystallization of messy conversations, a
real interpreter, and synthesis of new scripts for requests no cached workflow matches.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up
```

**Judge the product with the key set.** Without it, you are testing the pipeline, not the idea.

**There is a third path: a local model instead of either.** Set `LEY_KHAA_LLM=ollama` (with Ollama
running and a model pulled) for real language understanding with no API key at all — one local model
handles every stage, and the manifest names it (`ollama:<model>`) so a bundle never credits Claude
for local work. It is not a substitute for judging the product: output quality depends entirely on
the local model, small quantised models included, and vision stays text-only regardless of this
setting. The resolved backend is cached for the process's lifetime, so if the daemon wasn't reachable
or the model wasn't pulled at startup, fixing that afterwards (`ollama pull <model>`, starting the
daemon) needs a backend restart before it takes effect — the running process keeps using
`HeuristicLLM` and won't say so again. Under `docker compose up`, Ollama runs on the host rather than
in a container, so `LEY_KHAA_OLLAMA_HOST` needs to reach it as `http://host.docker.internal:11434` —
compose sets that default for you, but the daemon itself must also be started with
`OLLAMA_HOST=0.0.0.0` (it binds `127.0.0.1` by default) — on Linux, where the container reaches the
host at a bridge address, a loopback-bound daemon still refuses it, so without this the container
can't reach it at all. See
[Running without an API key](../README.md#running-without-an-api-key) in the README for the full
picture, including what happens when the daemon isn't reachable.

---

## 4. Your first task, end to end

This is the walkthrough that shows every part of the system doing its job. It works with or without
an API key.

### Send a request

```bash
curl -X POST http://localhost:8000/messages \
  -H 'Content-Type: application/json' \
  -d '{"text":"compare the bloomberg universe against the factset universe, csv",
       "conversation_id":"DEMO1"}'
```

```json
{
  "message_id": "d24a407f-...",
  "conversation_id": "DEMO1",
  "candidate_ids": ["d9bcb3e7-..."],
  "task_ids": ["2731625f-..."],
  "project": "default",
  "queued": true
}
```

Three things already happened: the message was crystallized into a **task candidate**, the candidate
was **routed into a project**, and the task was **queued** for a worker. `queued: true` means the
call returned before the work finished — the dispatcher runs it in the background.

### Watch it move

```bash
curl -s http://localhost:8000/tasks | python3 -m json.tool
```

Within a few seconds the task reaches `awaiting_approval`:

```
state: awaiting_approval | effective_mode: suggest
```

**This is the autonomy dial working, not a failure.** The engine scored the request and recommended
`suggest`, which parks it for a human rather than running it unattended. With a real API key and a
clearer request, it may recommend `auto` and run without stopping.

### Approve it

```bash
TID=<the task id>
curl -X POST "http://localhost:8000/tasks/$TID/approve"
```

It goes `executing` → `done`:

```
state: done | ok=True | Produced output.xlsx in 176 ms.
```

### Look at what it produced

```bash
curl -s "http://localhost:8000/tasks/$TID/bundle" | python3 -m json.tool
```

```
files: ['deliverable/output.xlsx', 'generator/attempt_1.py', 'generator/run.sh',
        'inputs/bloomberg_universe.csv', 'inputs/factset_universe.csv',
        'inputs/params.json', 'manifest.json']
sandbox: docker
```

That is the **Output Bundle**, and it is the point of the project. You get the deliverable, *the
code that produced it*, the exact inputs it ran against, and a manifest recording which sandbox and
which model did the work. Any result can be audited and re-run.

Download it:

```bash
curl -O -J "http://localhost:8000/tasks/$TID/bundle/deliverable"   # just the file
curl -O -J "http://localhost:8000/tasks/$TID/bundle/download"      # the whole bundle, zipped
```

---

## 5. What to try next

- **Replay a messy conversation.** `POST /simulate/{name}` replays a synthetic multi-message
  conversation through the real intake path — this is what the Task Crystallizer is actually for,
  and it is much more convincing than a single clean sentence. `GET /simulate` lists the fixtures.
- **Watch the dashboard while you do it.** Tasks, per-project queues, and the triage tray all
  update live.
- **Create a second project** with `POST /projects`, then send requests from different clients and
  watch them run concurrently in their own lanes.
- **Send a follow-up** to a conversation whose task is still running, and watch the amendment
  detector either fold it in or park it in the triage tray for you to decide.
- **Promote a workflow.** Once a bundle passes, `POST /tasks/{id}/promote` freezes its winning
  script into the registry, and the next matching request replays that proven code instead of
  paying for synthesis.

The full route table is in the [README](../README.md#run).

---

## 5.5 Connecting a real Slack or Discord channel (optional)

**Create a scratch workspace or server for this.** Never point it at anything work-adjacent — the
project's synthetic-data commitment does not survive a real channel.

**Slack**

1. Create an app at api.slack.com/apps → *From scratch*.
2. **Socket Mode** → enable it. That generates the **app-level token** (`xapp-…`) —
   `LEY_KHAA_SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → bot scopes `channels:history`, `chat:write`. Install to the workspace;
   the **bot token** (`xoxb-…`) is `LEY_KHAA_SLACK_BOT_TOKEN`.
4. **Event Subscriptions** → subscribe to `message.channels`.
5. Invite the bot to a channel, copy the channel id (right-click → *Copy link*; it is the `C…` part)
   into `LEY_KHAA_SLACK_CHANNELS`.

**Discord**

1. Create an application at discord.com/developers → *Bot*. The token is
   `LEY_KHAA_DISCORD_BOT_TOKEN`.
2. **Enable the MESSAGE CONTENT INTENT.** It is privileged and off by default. Without it every
   message arrives with empty content, the bot looks connected, and it silently ingests nothing.
3. Invite the bot with the `bot` scope and *Send Messages* / *Read Message History*.
4. Turn on Developer Mode in Discord, right-click the channel → *Copy Channel ID*, into
   `LEY_KHAA_DISCORD_CHANNELS`.

Then:

```bash
export LEY_KHAA_SLACK_BOT_TOKEN=xoxb-…
export LEY_KHAA_SLACK_APP_TOKEN=xapp-…
export LEY_KHAA_SLACK_CHANNELS=C0123456789
docker compose up
```

The startup log names every channel it is listening to. Post a request in that channel; watch the
task appear in the dashboard, answer the bot's question **in the thread**, and approve it in the
dashboard — approval stays there on purpose, because it releases work to run unattended.

On Discord the bot **starts a thread** from your message to ask its question, because Discord gives
a thread created that way the message's own id — which is what lets your reply route back to the
same task. Answer inside that thread.

If nothing happens, check the **Dead letters** panel first: a dropped message leaves a trace there
with the reason.

## 5.6 Pasting a screenshot into an allowlisted channel (optional)

Paste an image into a channel already in `LEY_KHAA_SLACK_CHANNELS` / `LEY_KHAA_DISCORD_CHANNELS`, or
into the dashboard directly. If it is a table, the resolver hands the generated script real CSV data
extracted from it; if it is anything else, its summary reaches the interpreter as context. Vision
runs once per image, keyed by a hash of its bytes — a re-drive or a second task quoting the same
screenshot reuses that result rather than reading the picture again, **as long as the source URL
still resolves**: the checkpoint holds no image bytes of its own, so re-fetching is how a re-drive
recomputes the cache key. Channel CDN URLs are not permanent (Discord's expire in about a day); once
one stops resolving, a human is asked instead of silently computing on the synthetic demo catalog --
but only when the image's own filename shares a token with the input it could be satisfying
(`holdings.png` for an input named "holdings"). A generic or auto-generated name -- a raw clipboard
paste (`image.png`) or a macOS screenshot (`Screenshot 2026-09-01 at 10.20.31.png`) -- isn't
recognized as any particular input, so the run proceeds on catalog data instead; the manifest's
`images` block still names the unread image, so that substitution is never silent either.

**No `ANTHROPIC_API_KEY` set?** The image is still carried, not read: it is recorded, credited to
the offline stand-in, and its name still reaches the prompt, but nothing looks at the picture and
the task proceeds on text alone. `docker compose up` still runs the demo end to end without one.

`LEY_KHAA_VISION=off` turns the whole path off — stronger than having no key: with no
`ANTHROPIC_API_KEY` the image is still fetched and handed to the offline stand-in, but `off` skips
the fetch itself, so no HTTP request goes out and no bot token is ever attached. Only images from
`LEY_KHAA_IMAGE_HOSTS` (Slack + Discord CDNs by default) are ever fetched, and a fetch is capped at
`LEY_KHAA_IMAGE_MAX_BYTES` (5 MB by default). See [Images](../README.md#images) in the README for
the full boundary and its stated limits.

## 6. Running without Docker

The fastest dev loop. The backend reads `DATABASE_URL`, so it runs on SQLite with no Postgres:

```bash
# backend
cd backend && pip install -e ".[dev]"
DATABASE_URL="sqlite:///./leykhaa.db" uvicorn ley_khaa.api.app:app --port 8000

# frontend, in a second shell
cd frontend && npm install && npm run dev
```

Generated code still runs in the Docker sandbox if a daemon is reachable; otherwise it falls back to
a subprocess sandbox, and the manifest records which one actually ran.

---

## 7. Troubleshooting

**`docker compose up` works but nothing intelligent happens.** No API key — see step 3.

**A task sits at `awaiting_approval` forever.** Working as designed: the autonomy dial parked it for
you. Approve it, or set the task's mode to `auto` with `POST /tasks/{id}/mode`.

**A task sits at `needs_clarification`.** The interpreter found a gap it will not guess about.
`GET /tasks/{id}` shows `open_question`; answer it with `POST /tasks/{id}/answer` and it re-enters as
a real message.

**The docker-parametrized tests fail with "No such file or directory" on a path that plainly
exists.** You are on Colima, Rancher, or Lima, which mount only `$HOME`, while pytest's temp files
live outside it. Create the directory *first* — if it does not exist, pytest silently falls back and
the error becomes an equally misleading "can't open file":

```bash
mkdir -p "$HOME/tmp" && TMPDIR="$HOME/tmp" python -m pytest -q
```

**Editable install resolves to the wrong path.** If you have used git worktrees and deleted one, a
venv installed from inside it will break with `ModuleNotFoundError: No module named 'ley_khaa'`.
Reinstall: `cd backend && pip install -e ".[dev]"`.

---

## 8. Running the tests

```bash
mkdir -p "$HOME/tmp"
cd backend  && TMPDIR="$HOME/tmp" python -m pytest -q   # 1030 passed, 0 skipped, 0 warnings
cd backend  && python -m mypy                            # CI fails the build on any error
cd frontend && npm test                                  # 58 passed
cd frontend && npm run typecheck                         # npm run build is transpile-only
```

The backend suite runs on SQLite by default, which needs nothing installed. It also runs against
**Postgres** — the database `docker compose up` deploys — and CI runs both lanes:

```bash
docker run -d --name leykhaa-test-pg -e POSTGRES_USER=ley -e POSTGRES_PASSWORD=ley \
  -e POSTGRES_DB=leykhaa -p 5432:5432 postgres:16
cd backend && DATABASE_URL=postgresql+psycopg://ley:ley@localhost:5432/leykhaa \
              TMPDIR="$HOME/tmp" python -m pytest -q --database=postgres
```

`--database` asserts only — it fails the run if the lane you asked for is not the lane you got, so a
lost `DATABASE_URL` cannot quietly re-run SQLite and report it as Postgres. The tests build their own
`ley_khaa_test` schema; [CONTRIBUTING](../CONTRIBUTING.md) says what that does and does not guarantee
about a database of your own.

**Run them in pytest's own order.** The suite is green in the default (alphabetical) file order and
has exactly one failure in reverse file order, identically on both lanes — a test that reloads the
config module and leaves a rebound `settings` object behind it. That is a test-cleanup defect, not a
defect in the shipped code; it is [backlog item
25](superpowers/specs/2026-08-28-phase-5-backlog.md), with a three-file reproducer.

Build the sandbox image once, or all 9 `[docker]` contract parameters silently skip:

```bash
docker build -t ley-khaa-sandbox backend/sandbox
```

---

## 9. Known limits

Every **feature** in the v1 definition of done (§11 of the design spec) has shipped, with one
deliberate narrowing worth calling out by name: §11's Model Router line reads "…with Ollama
fallback", which describes the router itself stepping down to Ollama per call when a tier is
unavailable. What 0.9.0 actually built (see
[Running without an API key](../README.md#running-without-an-api-key)) is Ollama as an
explicitly-selected backend for a whole run (`LEY_KHAA_LLM=ollama`), chosen once at startup — not
that automatic, per-call step-down, which remains backlog item 22 (see below). §11 also lists the
`v1.0.0` release tag itself as part of that definition of done, and that tag has not been cut yet
(see the phase table in the [README](../README.md#status)). Shipped is not the same as edge-free —
stated plainly so you do not go looking for something that isn't there:

- **The Ollama offline fallback is text-only.** Vision still requires `ANTHROPIC_API_KEY` regardless
  of which LLM backend is selected — see [Images](../README.md#images).
- **The Ollama backend is chosen once, at startup — there is no runtime step-down.** A Claude call
  that fails is not retried on Ollama, and vice versa (backlog item 22).
- **Output quality on the Ollama path depends entirely on the local model.** A small quantised model
  produces weaker specs and scripts than Opus, and nothing detects or warns about that beyond naming
  the model in the manifest.

The channel adapters shipped in 0.7.0 (see §5.5), with these limits:

- Approve, reject and mode override are dashboard actions. There are no interactive buttons, so a
  phone-only workflow is not possible.
- Notification is best-effort with dead-lettering, not a durable outbox.
- Dead-letter payloads are scrubbed, not sanitised: the redactor recognises Slack `xox*`/`xapp*`
  prefixes and `Bearer` headers, and nothing else — a raw base64 secret has no distinctive shape.
  Nothing writes one into a payload today; treat the panel as diagnostics.
- Non-image attachments are carried, not understood — a PDF or a spreadsheet reaches the interpreter
  as a URL, never fetched. Images are read (see §5.6 above), except with no `ANTHROPIC_API_KEY`,
  where an image is carried the same way.
- One workspace per platform, and threads only — no DMs.

**v0.10.0 closed twelve of the entries in
[the Phase 5 backlog](superpowers/specs/2026-08-28-phase-5-backlog.md)** — among them the three that
used to be listed here: a poisoned task now notifies when it fails, a second clarifying question is
delivered, and `dead_letters` has a row cap. Six entries stay open **by decision**, each with its
reason written down in that file: memory does not learn paraphrases the way the registry learns
aliases (1), the recall candidate cap wants measurement before it is changed (2), task memory has no
management surface (3), a same-backend vision failure stays frozen under that image's digest (20),
vision is text-only on the Ollama path (21), and there is no runtime step-down between backends (22).
Ten new entries (23–32) were filed the same way — mostly about the new gates' own blind spots, such
as migrations still being exercised on SQLite only.
