import csv

from openpyxl import Workbook

from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.validator import validate
from ley_khaa.executor.workspace import Workspace
from ley_khaa.interpreter.spec import TaskSpec


def _spec(output_format="xlsx") -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=["a", "b"], operation="set_difference",
        output_format=output_format, certainty=0.9,
    )


def _workspace(tmp_path) -> Workspace:
    workspace = Workspace.create(tmp_path, "t1")
    (workspace.inputs_dir / "a.csv").write_text("ticker\nAAA\n")
    return workspace


def _ok_result() -> SandboxResult:
    return SandboxResult(exit_code=0, stdout="done", stderr="", duration_ms=5, timed_out=False)


def _xlsx(workspace, name="output.xlsx", rows=1):
    book = Workbook()
    book.active.append(["ticker"])
    for index in range(rows):
        book.active.append([f"SYN{index}"])
    book.save(workspace.deliverable_dir / name)


def _csv(workspace, name="output.csv", rows=1):
    with (workspace.deliverable_dir / name).open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["ticker"])
        for index in range(rows):
            writer.writerow([f"SYN{index}"])


def test_a_good_run_passes_every_check(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace)
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert verdict.ok
    assert all(verdict.checks.values())


def test_a_timeout_is_reported_as_a_timeout_not_as_a_failure(tmp_path):
    """A killed script also has a non-zero exit code, so the more specific
    reason has to win or the human is told the wrong thing."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    result = SandboxResult(exit_code=-1, stdout="", stderr="killed", duration_ms=1, timed_out=True)
    verdict = validate(_spec(), workspace, result, hashes)
    assert not verdict.ok
    assert "too long" in verdict.reason
    assert verdict.checks["within_time_limit"] is False


def test_a_crash_is_reported_plainly(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    result = SandboxResult(
        exit_code=1, stdout="", stderr="Traceback...\nKeyError: 'ticker'", duration_ms=3,
        timed_out=False,
    )
    verdict = validate(_spec(), workspace, result, hashes)
    assert not verdict.ok
    # The traceback belongs in the bundle, never in the question put to a human.
    assert "Traceback" not in verdict.reason
    assert "KeyError" not in verdict.reason


def test_a_clean_exit_with_no_output_file_fails(tmp_path):
    workspace = _workspace(tmp_path)
    verdict = validate(_spec(), workspace, _ok_result(), workspace.input_hashes())
    assert not verdict.ok
    assert verdict.checks["deliverable_exists"] is False


def test_an_empty_output_file_fails(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    (workspace.deliverable_dir / "output.xlsx").write_bytes(b"")
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["deliverable_not_empty"] is False


def test_the_wrong_format_fails_and_says_which(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace)
    verdict = validate(_spec("xlsx"), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert "output.csv" in verdict.reason


def test_an_unrecognised_format_is_not_second_guessed(tmp_path):
    """expected_suffixes() has no opinion here, and a check with no opinion must
    not reject a perfectly good deliverable."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace)
    verdict = validate(_spec("a nicely formatted table"), workspace, _ok_result(), hashes)
    assert verdict.ok


def test_a_script_that_rewrote_its_inputs_fails(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace)
    (workspace.inputs_dir / "a.csv").write_text("ticker\nTAMPERED\n")
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["inputs_unmodified"] is False
    assert "reproduc" in verdict.reason


def test_a_header_only_spreadsheet_has_no_rows(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace, rows=0)
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["has_rows"] is False


def test_a_header_only_csv_has_no_rows(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace, rows=0)
    verdict = validate(_spec("csv"), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["has_rows"] is False


def test_a_non_tabular_deliverable_is_not_row_counted(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    (workspace.deliverable_dir / "output.md").write_text("# report\n\nall good\n")
    verdict = validate(_spec("markdown"), workspace, _ok_result(), hashes)
    assert verdict.ok


def test_a_symlinked_deliverable_fails_and_says_so(tmp_path):
    """`os.symlink("/etc/hosts", "deliverable/output.csv")` is one line inside a
    synthesized script. Following it would give a PASSING verdict, put the task
    in DONE, and have the manifest attest a sha256 over bytes the run never
    wrote — while the API refused to serve the file, so the bundle would
    contradict itself."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    outside = tmp_path / "outside.csv"
    outside.write_text("ticker\nSECRET\n")
    (workspace.deliverable_dir / "output.csv").symlink_to(outside)

    verdict = validate(_spec("csv"), workspace, _ok_result(), hashes)

    assert not verdict.ok
    assert verdict.checks["deliverable_is_a_real_file"] is False
    # And the reason names what actually happened, rather than reporting the
    # empty directory that dropping the link leaves behind.
    assert "link" in verdict.reason
    assert "output.csv" in verdict.reason
    assert workspace.deliverables() == []


def test_a_link_alongside_a_real_deliverable_still_fails(tmp_path):
    """One planted link is enough to make the bundle untrustworthy, even when
    the run also produced something genuine."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace.deliverable_dir / "aaa-link.xlsx").symlink_to(outside)

    verdict = validate(_spec(), workspace, _ok_result(), hashes)

    assert not verdict.ok
    assert "aaa-link.xlsx" in verdict.reason
