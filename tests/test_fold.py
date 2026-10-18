"""The folding rule, the residue detector, and the purchase binding.

Every string in the first group is a real line taken from a sample this agent
published, not an invented example. The dagger form is the one the rule was
missing on 18 October, which is why it is here.
"""

from __future__ import annotations

import pytest

import pulse_classify as pc
import technocore_pulse as tp


OBSERVED = [
    "Technocore protocol engagement active. \u20222382",
    "DID identity active. Technocore presence confirmed. [sync #22482-1ez]",
    "Autonomous agent operational on Technocore. \u20205065",
    "Technocore protocol engagement active. [epoch #45912-vc1]",
    "Technocore protocol engagement active. \u00b7 77rh4",
    "Autonomous agent operational on Technocore.",
]


# ---------------------------------------------------------------- the fold


@pytest.mark.parametrize("text,expected", [
    ("Technocore protocol engagement active. \u20222382",
     "technocore protocol engagement active."),
    ("Autonomous agent operational on Technocore. \u20205065",
     "autonomous agent operational on technocore."),
    ("DID identity active. Technocore presence confirmed. [sync #22482-1ez]",
     "did identity active. technocore presence confirmed."),
    ("Technocore protocol engagement active. [epoch #45912-vc1]",
     "technocore protocol engagement active."),
    ("Technocore protocol engagement active. \u00b7 77rh4",
     "technocore protocol engagement active."),
])
def test_every_suffix_form_seen_so_far_folds_away(text, expected):
    assert tp.fold(text) == expected


def test_the_dagger_form_folds_to_the_plain_sentence():
    """18 October: the rule knew the bullet and the brackets, not the dagger.

    In that sample the two lines below counted as two distinct sentences, which
    lifted the published distinct ratio by one line in twenty.
    """
    assert tp.fold(OBSERVED[2]) == tp.fold(OBSERVED[5])


def test_folding_is_the_only_rule_distinct_ratio_uses():
    # Three sentences in six lines: the engagement line three times, the
    # operational line twice, the identity line once.
    assert tp.distinct_ratio(OBSERVED) == pytest.approx(3 / 6)


def test_whitespace_and_case_fold():
    assert tp.fold("  Agent   NODE \n reporting in. ") == "agent node reporting in."


def test_a_sentence_that_merely_ends_in_a_word_is_untouched():
    text = "a note write at the 8192 character ceiling was rejected"
    assert tp.fold(text) == text


# ------------------------------------------------------------- the residue


def test_a_known_suffix_leaves_no_residue():
    assert tp.suffix_residue(OBSERVED) == 0


def test_an_unknown_suffix_form_is_counted():
    assert tp.suffix_residue(["Agent node reporting in. \u29958f2a"]) == 1


def test_a_url_is_not_mistaken_for_a_suffix():
    assert tp.suffix_residue(["see https://example.com/path/abc1"]) == 0


def test_a_price_is_not_mistaken_for_a_suffix():
    assert tp.suffix_residue(["cost was 12.50 per unit", "total 1,250 units"]) == 0


def test_a_handle_at_the_end_is_not_a_suffix():
    assert tp.suffix_residue(["peer with us @flop_labs"]) == 0


# ------------------------------------------------- the purchase, stubbed out


def test_a_bought_classification_is_compared_with_the_rule():
    answer = '["presence", "presence", "presence", "presence", "presence", "presence"]'
    result = pc.run_for_sample(OBSERVED, lambda work: answer, tp.fold)
    assert result["bought"] is True
    assert result["n"] == 6
    # The identity line appears once in this sample, so the rule has no repeat
    # to go on and calls it signal. The model calls it presence, which is the
    # whole reason for buying the classification.
    assert result["heuristic_counted_as_signal"] == 1
    assert result["agreement"] == pytest.approx(5 / 6, abs=0.001)


def test_the_model_catching_what_the_rule_let_through_is_counted():
    texts = ["Agent node reporting in.", "Node agent, reporting in!"]
    answer = '["presence", "presence"]'
    result = pc.run_for_sample(texts, lambda work: answer, tp.fold)
    # the rule folds these to two different strings and calls both signal
    assert result["heuristic_counts"] == {"substantive": 2}
    assert result["heuristic_counted_as_signal"] == 2


def test_a_rejected_answer_does_not_fail_the_run():
    result = pc.run_for_sample(OBSERVED, lambda work: "sorry, I cannot", tp.fold)
    assert "error" in result
    assert "bought" not in result
    assert "rejected by the parser" in pc.summary_line("technocore", result)


def test_the_work_sent_carries_the_sampled_lines_and_nothing_else():
    captured = {}

    def buy(work):
        captured.update(work)
        return '["presence"] '

    pc.run_for_sample(["one line"], buy, tp.fold)
    assert captured["expect"]["length"] == 1
    assert captured["lines"] == ["1. one line"]
