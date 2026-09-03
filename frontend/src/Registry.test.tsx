import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import Registry from "./Registry";
import type { Workflow } from "./api";

const workflow = (overrides: Partial<Workflow> = {}): Workflow => ({
  name: "set_difference",
  description: "Diffs two universes and writes what's missing.",
  operation_aliases: ["set_difference"],
  output_format: "xlsx",
  inputs: [{ role: "dataset", suffixes: [".csv"] }],
  origin: "seed",
  promoted_from_task_id: null,
  runs_ok: 4,
  runs_failed: 0,
  quarantined: false,
  source_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567",
  ...overrides,
});

const okJson = (body: unknown, status = 200) => ({ ok: true, status, json: async () => body });

// Fakes the real registry endpoints closely enough that a click's effect
// (quarantine cleared, row removed) is visible in the next GET, the same way
// the real backend would report it — not just a call we counted.
function stubRegistry(initial: Workflow[]) {
  let current = [...initial];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "DELETE") {
        const name = decodeURIComponent(u.split("/registry/")[1]);
        current = current.filter((w) => w.name !== name);
        return { ok: true, status: 204, json: async () => undefined };
      }
      if (method === "POST" && u.includes("/unquarantine")) {
        const name = decodeURIComponent(u.split("/registry/")[1].split("/unquarantine")[0]);
        current = current.map((w) => (w.name === name ? { ...w, quarantined: false } : w));
        return okJson(current.find((w) => w.name === name));
      }
      return okJson(current);
    }),
  );
}

const fetchCalls = () => (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls;

afterEach(cleanup);

test("lists each workflow with its origin and usage", async () => {
  stubRegistry([
    workflow({
      name: "set_difference",
      description: "Diffs two universes and writes what's missing.",
      origin: "seed",
      runs_ok: 4,
      runs_failed: 1,
    }),
  ]);
  render(<Registry />);

  expect(await screen.findByText("set_difference")).toBeTruthy();
  expect(screen.getByText(/Diffs two universes/)).toBeTruthy();
  expect(screen.getByText(/seed/)).toBeTruthy();
  expect(screen.getByText(/4 ok/)).toBeTruthy();
  expect(screen.getByText(/1 failed/)).toBeTruthy();
});

test("marks a quarantined workflow and offers to clear it", async () => {
  stubRegistry([workflow({ name: "flaky", quarantined: true, runs_failed: 3 })]);
  render(<Registry />);

  expect(await screen.findByText(/quarantined/i)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /clear quarantine/i }));

  await waitFor(() => {
    const call = fetchCalls().find(([, init]) => init?.method === "POST");
    expect(call).toBeTruthy();
    expect(call![0]).toContain("/registry/flaky/unquarantine");
  });
  // The click's effect surfaces through the re-fetch, not just the request.
  await waitFor(() => expect(screen.queryByText(/quarantined/i)).toBeNull());
});

test("does not offer to clear a healthy workflow", async () => {
  stubRegistry([workflow({ name: "set_difference", quarantined: false })]);
  render(<Registry />);

  expect(await screen.findByText("set_difference")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /clear quarantine/i })).toBeNull();
});

test("removes a workflow and refreshes the list", async () => {
  stubRegistry([workflow({ name: "set_difference" })]);
  render(<Registry />);
  await screen.findByText("set_difference");

  fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
  fireEvent.click(screen.getByRole("button", { name: /really delete\?/i }));

  await waitFor(() => expect(screen.queryByText("set_difference")).toBeNull());

  const calls = fetchCalls();
  const deleteCall = calls.find(([, init]) => init?.method === "DELETE");
  expect(deleteCall![0]).toContain("/registry/set_difference");
  const getCalls = calls.filter(([, init]) => !init?.method || init.method === "GET");
  // The initial load plus a refresh after delete — not just the DELETE itself.
  expect(getCalls.length).toBeGreaterThanOrEqual(2);
});

test("says so when the registry is empty", async () => {
  stubRegistry([]);
  render(<Registry />);

  expect(await screen.findByText(/no workflows/i)).toBeTruthy();
});

test("surfaces a fetch failure", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500, json: async () => null })));
  render(<Registry />);

  expect(await screen.findByText(/fetchWorkflows failed/i)).toBeTruthy();
});

test("surfaces a failed unquarantine without losing the quarantine badge", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "POST" && u.includes("/unquarantine")) {
        return { ok: false, status: 500, json: async () => null };
      }
      return okJson([workflow({ name: "flaky", quarantined: true, runs_failed: 3 })]);
    }),
  );
  render(<Registry />);

  expect(await screen.findByText(/quarantined/i)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /clear quarantine/i }));

  expect(await screen.findByText(/unquarantineWorkflow failed/i)).toBeTruthy();
  // Nothing was optimistically cleared — the badge is still there.
  expect(screen.getByText(/quarantined/i)).toBeTruthy();
});

test("re-fetches only when the refresh signal changes, not on every render", async () => {
  // App lifts a refresh signal so a promotion elsewhere on the page can
  // trigger a re-fetch here (see App.test.tsx). Registry's effect must
  // depend on that signal — but `load` sits in the same dependency array
  // (it is used inside the effect), so if `load` is not memoised with
  // useCallback, its identity changes every render and the effect re-fires
  // on every render regardless of the signal: an infinite refetch loop, not
  // just wasted renders. This test pins BOTH halves as one change, per the
  // controller's ruling that the useCallback is only observable in
  // composition with the refresh signal.
  stubRegistry([workflow({ name: "set_difference" })]);
  const gets = () => fetchCalls().filter(([, init]) => !init?.method || init.method === "GET").length;

  const { rerender } = render(<Registry refreshSignal={0} />);
  await screen.findByText("set_difference");
  const afterMount = gets();

  // Re-rendering with the SAME signal value must not re-fetch.
  rerender(<Registry refreshSignal={0} />);
  await new Promise((r) => setTimeout(r, 0));
  expect(gets()).toBe(afterMount);

  // Changing the signal must re-fetch exactly once more.
  rerender(<Registry refreshSignal={1} />);
  await waitFor(() => expect(gets()).toBe(afterMount + 1));
});

test("shows why a delete failed and leaves the row in place", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = init?.method ?? "GET";
      if (method === "DELETE") {
        return { ok: false, status: 500, json: async () => null };
      }
      return okJson([workflow({ name: "set_difference" })]);
    }),
  );
  render(<Registry />);
  await screen.findByText("set_difference");

  fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
  fireEvent.click(screen.getByRole("button", { name: /really delete\?/i }));

  expect(await screen.findByText(/deleteWorkflow failed/i)).toBeTruthy();
  // Still present — the failed delete did not remove it.
  expect(screen.getByText("set_difference")).toBeTruthy();
  // The two-step confirm dropped back to idle rather than staying stuck.
  expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /really delete\?/i })).toBeNull();
});
