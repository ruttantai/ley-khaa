import pytest

from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.orm import WorkflowRow
from ley_khaa.registry.fingerprint import (
    formats_agree,
    fingerprint_candidates,
    normalize_operation,
)


def _spec(operation="set_difference", output_format="csv", inputs=("a", "b")):
    return TaskSpec(
        intent="compare the two universes",
        inputs=list(inputs),
        operation=operation,
        output_format=output_format,
        certainty=0.9,
    )


def _workflow(name="set_difference", aliases=("set_difference",), output_format="csv", roles=2):
    return WorkflowRow(
        id=name,
        name=name,
        description="",
        operation_aliases=list(aliases),
        output_format=output_format,
        inputs=[{"role": f"r{i}", "suffixes": [".csv"]} for i in range(roles)],
        source="",
        source_sha256="",
        origin="seed",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Set Difference", "set_difference"),
        ("  set-difference  ", "set_difference"),
        ("set__difference", "set_difference"),
        ("SET DIFFERENCE!!", "set_difference"),
        ("", ""),
    ],
)
def test_operations_normalize_to_one_shape(raw, expected):
    assert normalize_operation(raw) == expected


def test_excel_and_xlsx_are_the_same_format():
    """formats.py already knows this. Comparing raw strings would forget it and
    re-synthesize a workflow we already have, purely over a synonym."""
    assert formats_agree("excel", "xlsx") is True
    assert formats_agree("spreadsheet", "xlsx") is True
    assert formats_agree("csv", "xlsx") is False


def test_an_unknown_format_never_agrees_with_anything():
    """expected_suffixes() returns () for a format it does not recognise. Two
    unknown formats matching each other would let any unrecognised word match
    any other, which is worse than a cache miss."""
    assert formats_agree("interpretive dance", "interpretive dance") is False
    assert formats_agree("interpretive dance", "csv") is False


def test_a_candidate_needs_the_operation_the_format_and_the_arity():
    workflows = [
        _workflow(name="right"),
        _workflow(name="wrong_operation", aliases=("summary_stats",)),
        _workflow(name="wrong_format", output_format="docx"),
        _workflow(name="wrong_arity", roles=1),
    ]
    assert [w.name for w in fingerprint_candidates(_spec(), workflows)] == ["right"]


def test_a_paraphrased_operation_is_a_miss_not_a_guess():
    """Stage 2 exists for this. Stage 1 guessing here is how a request gets run
    by code that was proven for a different job."""
    assert fingerprint_candidates(_spec(operation="compare_lists"), [_workflow()]) == []


def test_a_quarantined_workflow_is_never_a_candidate():
    workflow = _workflow()
    workflow.quarantined = True
    assert fingerprint_candidates(_spec(), [workflow]) == []
