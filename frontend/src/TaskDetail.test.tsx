import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TaskDetail from "./TaskDetail";
import type { Task } from "./api";

const task = (overrides: Partial<Task> = {}): Task => ({
  id: "t1",
  project: "default",
  state: "awaiting_approval",
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
  ...overrides,
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => task({ state: "done" }) })));
});
afterEach(cleanup);

test("shows the recommendation and its plain-English reason", () => {
  render(<TaskDetail task={task()} onChanged={() => {}} />);
  expect(screen.getByText(/I suggest Co-pilot/)).toBeTruthy();
});

test("shows the interpreted spec", () => {
  render(<TaskDetail task={task()} onChanged={() => {}} />);
  expect(screen.getByDisplayValue("set_difference")).toBeTruthy();
  expect(screen.getByDisplayValue("xlsx")).toBeTruthy();
});

test("approving calls the API and reports the change", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  fireEvent.click(screen.getByText("Approve"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(String((globalThis.fetch as never as { mock: { calls: string[][] } }).mock.calls[0][0]))
    .toContain("/tasks/t1/approve");
});

test("the dial marks the mode actually in force", () => {
  render(<TaskDetail task={task({ mode_override: "auto", effective_mode: "auto" })} onChanged={() => {}} />);
  expect(screen.getByLabelText("Auto").getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByLabelText("Co-pilot").getAttribute("aria-pressed")).toBe("false");
});

test("a blocked task shows its question and an answer box instead of approval", () => {
  render(
    <TaskDetail
      task={task({ state: "needs_clarification", open_question: "Excel or CSV?" })}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByText(/Excel or CSV\?/)).toBeTruthy();
  expect(screen.getByPlaceholderText(/answer/i)).toBeTruthy();
  expect(screen.queryByText("Approve")).toBeNull();
});

test("a finished task offers no actions", () => {
  render(<TaskDetail task={task({ state: "done" })} onChanged={() => {}} />);
  expect(screen.queryByText("Approve")).toBeNull();
  expect(screen.queryByText("Reject")).toBeNull();
  // I3: the mode dial must be gated the same way approve/reject/edit_spec are
  // on the backend — a done task cannot have its autonomy mode changed.
  expect(screen.queryByLabelText("Auto")).toBeNull();
  expect(screen.queryByLabelText("Suggest")).toBeNull();
  expect(screen.queryByLabelText("Co-pilot")).toBeNull();
});

test("a blocked task still offers a way to kill it", () => {
  // M3: needs_clarification -> failed is legal server-side; the human facing
  // a question they cannot answer must have a Reject button.
  render(
    <TaskDetail
      task={task({ state: "needs_clarification", open_question: "Excel or CSV?" })}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByText("Reject")).toBeTruthy();
});

test("rejecting from needs_clarification calls the reject endpoint", async () => {
  const onChanged = vi.fn();
  render(
    <TaskDetail
      task={task({ state: "needs_clarification", open_question: "Excel or CSV?" })}
      onChanged={onChanged}
    />,
  );
  fireEvent.click(screen.getByText("Reject"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(String((globalThis.fetch as never as { mock: { calls: string[][] } }).mock.calls[0][0]))
    .toContain("/tasks/t1/reject");
});

test("editing a spec field patches it", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  const field = screen.getByLabelText("output_format") as HTMLInputElement;
  fireEvent.change(field, { target: { value: "csv" } });
  fireEvent.blur(field);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const [url, init] = (globalThis.fetch as never as { mock: { calls: [string, RequestInit][] } })
    .mock.calls[0];
  expect(String(url)).toContain("/tasks/t1/spec");
  expect(init.method).toBe("PATCH");
});

test("M6: clearing a non-nullable field sends an empty string, not null", async () => {
  // output_format is non-nullable server-side (TaskSpec); sending null 422s.
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  const field = screen.getByLabelText("output_format") as HTMLInputElement;
  fireEvent.change(field, { target: { value: "" } });
  fireEvent.blur(field);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const [, init] = (globalThis.fetch as never as { mock: { calls: [string, RequestInit][] } })
    .mock.calls[0];
  expect(JSON.parse(init.body as string)).toEqual({ patch: { output_format: "" } });
});

test("M6: clearing the nullable recipient field still sends null", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  const field = screen.getByLabelText("recipient") as HTMLInputElement;
  fireEvent.change(field, { target: { value: "" } });
  fireEvent.blur(field);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  const [, init] = (globalThis.fetch as never as { mock: { calls: [string, RequestInit][] } })
    .mock.calls[0];
  expect(JSON.parse(init.body as string)).toEqual({ patch: { recipient: null } });
});
