from ley_khaa.memory.fingerprint import STOPWORDS, request_fingerprint


def test_the_same_request_fingerprints_the_same():
    assert request_fingerprint(["Compare the Bloomberg universe against FactSet"]) == \
        request_fingerprint(["Compare the Bloomberg universe against FactSet"])


def test_politeness_and_word_order_do_not_change_the_fingerprint():
    """A repeat request is rarely typed identically. Courtesy words and order
    are exactly the noise a fingerprint has to see through."""
    a = request_fingerprint(["Hi! Can you compare the Bloomberg universe against FactSet, please?"])
    b = request_fingerprint(["compare FactSet against the bloomberg universe. Thanks!"])
    assert a == b


def test_a_different_request_fingerprints_differently():
    a = request_fingerprint(["compare the bloomberg universe against factset"])
    b = request_fingerprint(["summarise the holdings by sector"])
    assert a != b


def test_bare_numbers_are_dropped_so_a_date_does_not_split_a_repeat():
    """"the usual universe check" arriving on the 3rd and the 10th is one
    remembered request, not two."""
    a = request_fingerprint(["run the universe check for 2026-08-03"])
    b = request_fingerprint(["run the universe check for 2026-08-10"])
    assert a == b


def test_an_empty_request_has_no_fingerprint():
    """Empty must never collide with empty and match every other blank task."""
    assert request_fingerprint([]) == ""
    assert request_fingerprint(["", "   "]) == ""


def test_the_stopword_list_is_pinned():
    """Pinned deliberately: quietly adding a word re-fingerprints every stored
    memory, and every past request silently stops matching."""
    assert "please" in STOPWORDS and "the" in STOPWORDS
    assert "universe" not in STOPWORDS
    assert len(STOPWORDS) == 52
