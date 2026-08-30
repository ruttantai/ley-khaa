# Getting started

A first run of ley-khaa, from a fresh clone to a finished task with a downloadable deliverable.

Every step below was run on macOS (Apple Silicon, Docker via Colima) against `v0.6.0`. Where a step
produces output worth checking, the real output is shown.

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
cd backend  && TMPDIR="$HOME/tmp" python -m pytest -q   # 639 passed, 0 skipped, 0 warnings
cd frontend && npm test                                  # 49 passed
cd frontend && npm run typecheck                         # npm run build is transpile-only
```

Build the sandbox image once, or all 9 `[docker]` contract parameters silently skip:

```bash
docker build -t ley-khaa-sandbox backend/sandbox
```

---

## 9. What is not built yet

Stated plainly so you do not go looking for it:

- **Slack and Discord adapters.** The intake gateway is adapter-shaped and the message model already
  carries `source` and `client`, but the only adapters that exist are the HTTP endpoint and the
  conversation simulator. There is nothing to point a real Slack workspace at yet.
- **Vision intake.** The model router has a `VISION_EXTRACTION` stage with a token budget, but
  nothing calls it — attachments reach the interpreter as *names*, not images.
- **Ollama offline fallback.** Not started.

These are the remaining items in the v1 definition of done (§11 of the design spec).
