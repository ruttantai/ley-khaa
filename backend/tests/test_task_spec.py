import pytest
from pydantic import ValidationError

from ley_khaa.interpreter.spec import TaskSpec


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two security universes",
        inputs=["bloomberg_universe", "factset_universe"],
        operation="set_difference",
        output_format="xlsx",
        recipient="boss",
        urgency="normal",
        missing_fields=[],
        source_message_ids=["m1", "m2"],
        certainty=0.9,
    )
    return TaskSpec(**{**base, **overrides})


def test_a_complete_spec_validates():
    spec = _spec()
    assert spec.operation == "set_difference"
    assert spec.certainty == 0.9


def test_certainty_is_bounded():
    with pytest.raises(ValidationError):
        _spec(certainty=1.4)


def test_urgency_is_constrained():
    with pytest.raises(ValidationError):
        _spec(urgency="whenever")


def test_unknown_fields_are_rejected():
    """A typo in an edit_spec patch must 422, not vanish silently."""
    with pytest.raises(ValidationError):
        _spec(outupt_format="xlsx")


def test_optional_fields_default_sensibly():
    spec = TaskSpec(intent="x", operation="y", output_format="z", certainty=0.5)
    assert spec.inputs == []
    assert spec.recipient is None
    assert spec.urgency == "normal"
    assert spec.missing_fields == []
