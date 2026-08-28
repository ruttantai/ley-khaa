import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import type { Task } from "./api";

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

function stubApi(candidates: unknown[], workflows: unknown[] = []) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const u = String(url);
      const body = u.includes("/candidates") ? candidates : u.includes("/registry") ? workflows : [task()];
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
