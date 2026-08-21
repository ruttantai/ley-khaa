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
  expect(screen.getByText("set_difference")).toBeTruthy();
  expect(screen.getByText("xlsx")).toBeTruthy();
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
});

test("editing a spec field patches it", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  const field = screen.getByLabelText("output_format") as HTMLInputElement;
  fireEvent.change(field, { target: { value: "csv" } });
  fireEvent.blur(field);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});
