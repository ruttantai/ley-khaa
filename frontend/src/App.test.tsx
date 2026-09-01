import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import type { Project, Task, TriageItem } from "./api";

const candidate = (state: string, title: string) => ({
  id: `c-${state}`,
  conversation_id: "conv-1",
  candidate_key: `k-${state}`,
  title,
  summary: title,
  state,
  message_ids: ["m1"],
  missing_fields: [],
  open_question: null,
  task_id: null,
});

const task = (overrides: Partial<Task> = {}): Task => ({
  id: "t1",
  project: "default",
  state: "done",
  title: "compare universes",
  spec: {
    intent: "compare two universes",
    inputs: ["bloomberg", "factset"],
    operation: "set_difference",
    output_format: "xlsx",
    recipient: "boss",
    urgency: "normal",
    missing_fields: [],
    source_message_ids: ["m1"],
    certainty: 0.9,
  },
  recommended_mode: "copilot",
  mode_override: null,
  effective_mode: "copilot",
  confidence: 0.9,
  risk: 0.45,
  autonomy_reason: "90% sure, medium risk — it delivers something to someone → I suggest Co-pilot",
  open_question: null,
  failure_reason: null,
  workspace_path: null,
  execution_verdict: null,
  remembered_from_task_id: null,
  familiarity: 0,
  ...overrides,
});

const project = (overrides: Partial<Project> = {}): Project => ({
  name: "acme",
  display_name: "Acme",
  description: "d",
  active: true,
  queue_depth: 1,
  in_flight: null,
  ...overrides,
});

// Deliberately not "compare universes" or anything else the task fixture
// above uses — App.test.tsx's other assertions query by exact task title,
// and a triage fixture that collided with it would make those queries
// ambiguous the same way the catch-all bug below once did.
const triageItem = (overrides: Partial<TriageItem> = {}): TriageItem => ({
  candidate_id: "c9",
  title: "flag mismatched totals",
  summary: "s",
  amends_task_id: "t9",
  amends_task_title: "reconcile ledgers",
  reason: "adds a check",
  confidence: 0.8,
  ...overrides,
});

function stubApi(
  candidates: unknown[],
  workflows: unknown[] = [],
  projects: unknown[] = [project()],
  triage: unknown[] = [triageItem()],
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      // Route POST /candidates/{id}/fold|separate before the GET /candidates
      // listing check below — otherwise a mutation call would match on
      // "/candidates" and be silently faked as a successful listing fetch,
      // rather than genuinely exercising (or failing) the mutation.
      if (method === "POST" && (u.includes("/fold") || u.includes("/separate"))) {
        return { ok: true, status: 200, json: async () => ({ id: "task-x" }) };
      }
      // BEFORE the catch-all below: without this branch /dead-letters falls
      // through to [task()] and the panel renders task objects as dead letters
      // — the same catch-all bug the comment above already records.
      const body = u.includes("/dead-letters")
        ? []
        : u.includes("/candidates")
          ? candidates
          : u.includes("/registry")
            ? workflows
            : u.includes("/projects")
              ? projects
              : u.includes("/triage")
                ? triage
                : [task()];
      return { ok: true, json: async () => body };
    }),
  );
}

beforeEach(() => stubApi([]));
afterEach(cleanup);

test("renders tasks from the API", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.getByText(/done/)).toBeTruthy();
});

test("Forming lists only candidates that are still forming", async () => {
  stubApi([
    candidate("crystallizing", "Universe reconciliation"),
    candidate("promoted", "Already a task"),
    candidate("abandoned", "Dropped request"),
  ]);
  render(<App />);
  await waitFor(() => expect(screen.getByText("Universe reconciliation")).toBeTruthy());
  expect(screen.queryByText("Already a task")).toBeNull();
  expect(screen.queryByText("Dropped request")).toBeNull();
});

test("opening a task shows its recommendation", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  fireEvent.click(screen.getByText("compare universes"));
  expect(screen.getByText(/I suggest Co-pilot/)).toBeTruthy();
});

test("renders the projects view and the triage tray", async () => {
  render(<App />);
  expect(await screen.findByText("Acme")).toBeTruthy();
  expect(await screen.findByText("flag mismatched totals")).toBeTruthy();
});

test("a parked amendment is listed in the Triage tray only, not under Forming", async () => {
  // Same candidate, both endpoints: GET /triage renders it as a decision to
  // make, GET /candidates still lists the row. Before awaiting_triage joined
  // TERMINAL_STATES the page showed it twice — once as a decision, once as
  // something still forming.
  stubApi(
    [
      candidate("awaiting_triage", "flag mismatched totals"),
      candidate("crystallizing", "Universe reconciliation"),
    ],
    [],
    [project()],
    [triageItem()],
  );
  render(<App />);
  await waitFor(() => expect(screen.getByText("Universe reconciliation")).toBeTruthy());
  expect(screen.getAllByText("flag mismatched totals")).toHaveLength(1);
});

test("a healthy dashboard shows no dead-letter panel", async () => {
  // Pins the `/dead-letters` branch in stubApi. Without it the URL falls
  // through to the catch-all's `[task()]`, and the panel renders TASK objects
  // as dead letters — a red "Dead letters (1)" heading on a healthy dashboard,
  // with every field undefined. The branch was previously load-bearing but
  // unpinned: deleting it broke no test.
  stubApi([]);
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.queryByText(/Dead letters/)).toBeNull();
});
