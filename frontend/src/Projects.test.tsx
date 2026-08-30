import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Projects from "./Projects";
import * as api from "./api";

beforeEach(() => vi.restoreAllMocks());

// Projects polls via setInterval — unmounting after each test clears that
// timer so it does not leak into later tests in the run.
afterEach(cleanup);

test("it shows each project's queue depth and its waiting state", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 2, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText("Acme")).toBeTruthy();
  expect(screen.getByText(/2 queued/)).toBeTruthy();
  // Queued with nothing running is a state of its own, distinct from idle
  // (nothing queued) and from running (something in flight) — collapsing it
  // into "idle" would hide a project that is actually waiting on its queue.
  expect(screen.getByText(/waiting/i)).toBeTruthy();
});

test("it names the task currently in flight", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: "task-7" },
  ]);
  render(<Projects />);
  // Asserting on the task id, not just the word "running" — a message that
  // said "running" without naming which task would be useless to a human.
  await waitFor(() => expect(screen.getByText(/task-7/)).toBeTruthy());
});

test("it says so when there is nothing queued", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText(/idle/i)).toBeTruthy();
});

test("shows a fetch failure", async () => {
  vi.spyOn(api, "fetchProjects").mockRejectedValue(new Error("network down"));
  render(<Projects />);
  expect(await screen.findByText(/network down/i)).toBeTruthy();
});

test("clears a previous error once a later poll succeeds", async () => {
  vi.useFakeTimers();
  try {
    vi.spyOn(api, "fetchProjects")
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce([
        { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: null },
      ]);
    render(<Projects />);
    // Flush the initial (rejecting) load.
    await act(async () => {});
    expect(screen.getByText(/network down/i)).toBeTruthy();

    // Advance past the 3s poll interval so the second, successful call lands.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    // A polling view that stays on a stale error forever, even once the
    // backend recovers, is worse than useless — it must clear on success.
    expect(screen.getByText("Acme")).toBeTruthy();
    expect(screen.queryByText(/network down/i)).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});
