from ley_khaa.executor.formats import deliverable_filename, expected_suffixes


def test_spoken_format_names_map_to_suffixes():
    assert expected_suffixes("xlsx") == (".xlsx",)
    assert expected_suffixes("Excel") == (".xlsx",)
    assert expected_suffixes("csv") == (".csv",)
    assert expected_suffixes("word") == (".docx",)


def test_an_unrecognised_format_has_no_opinion():
    """Rejecting a good deliverable because the request described it in words we
    did not anticipate would be worse than not checking at all."""
    assert expected_suffixes("a nicely formatted table") == ()
    assert expected_suffixes("") == ()


def test_deliverable_filename_follows_the_format():
    assert deliverable_filename("xlsx") == "output.xlsx"
    assert deliverable_filename("csv") == "output.csv"
    assert deliverable_filename("something odd") == "output.txt"
