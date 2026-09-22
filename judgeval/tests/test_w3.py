from judgeval.w3.equivalence import feasible_tables, mde, summarize_margins, tost_passes


def test_point_difference_is_identified_and_the_table_is_not():
    tables = feasible_tables(18, 20, 24)
    assert len(tables) > 1
    diffs = {(ao - bo) / 24 for _, ao, bo, _ in tables}
    assert diffs == {-(2 / 24)}


def test_a_point_estimate_outside_the_margin_cannot_be_equivalence():
    block = summarize_margins(18, 20, 24)
    assert block["point_outside_margin"]
    assert block["tost_any_table"] is False
    assert block["all_nonsignificant_unadjusted"] is True


def test_a_zero_difference_with_many_discordant_pairs_is_not_equivalence():
    block = summarize_margins(12, 12, 24)
    assert block["point_diff"] == 0
    assert block["tost_all_tables"] is False
    assert block["all_nonsignificant_unadjusted"] is True


def test_tost_uses_the_90_percent_interval():
    assert tost_passes((-0.04, 0.04))
    assert not tost_passes((-0.06, 0.04))


def test_mde_at_n24_is_larger_than_a_5_point_margin_at_ordinary_discordance():
    assert mde(0.25, 24) > 0.20
    assert mde(0.0, 24) == 0.0
