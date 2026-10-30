"""Grounding: figures come from named result cells, nothing else."""

from analyst.grounding import format_value, render

ONE_ROW = [{"n": 45101, "late_share": 0.0812}]
TWO_ROWS = [{"year": 2017, "n": 45101}, {"year": 2018, "n": 54011}]


def test_fills_placeholders_from_one_row() -> None:
    out = render("There were {{n}} orders, {{ late_share }} late.", ONE_ROW)
    assert out.ok
    assert out.text == "There were 45,101 orders, 0.08 late."


def test_fills_indexed_placeholders() -> None:
    out = render("{{rows[0].n}} in {{rows[0].year}} and {{rows[1].n}} in {{rows[1].year}}.", TWO_ROWS)
    assert out.ok
    assert out.text == "45,101 in 2017 and 54,011 in 2018."


def test_missing_column_fails() -> None:
    out = render("{{total}} orders", ONE_ROW)
    assert out.failures == ["{{total}} names column total, which is not in the result"]


def test_missing_row_fails() -> None:
    out = render("{{rows[2].n}}", TWO_ROWS)
    assert len(out.failures) == 1 and "row 2" in out.failures[0]


def test_unindexed_placeholder_on_many_rows_fails() -> None:
    out = render("{{n}} orders", TWO_ROWS)
    assert len(out.failures) == 1 and "exactly one result row" in out.failures[0]


def test_free_number_fails() -> None:
    out = render("About 45,000 orders, {{n}} exactly.", ONE_ROW)
    assert out.failures == ["literal number 45,000 in the text is not a result cell"]


def test_year_is_allowed() -> None:
    assert render("In 2017 there were {{n}} orders.", ONE_ROW).ok


def test_number_copied_from_the_question_is_allowed() -> None:
    question = "What are the top 5 categories?"
    assert render("The top 5 categories: {{rows[0].n}} ...", TWO_ROWS, question).ok
    assert not render("The top 6 categories: {{rows[0].n}} ...", TWO_ROWS, question).ok


def test_format_value() -> None:
    assert format_value(None) == "n/a"
    assert format_value(1234567) == "1,234,567"
    assert format_value(3.0) == "3"
    assert format_value(0.0812) == "0.08"
    assert format_value("delivered") == "delivered"


PCT_ROW = [{"year": 2017, "n": 45101, "late_share_pct": 8.11, "growth_pct": 140.35, "missing_pct": None}]


def test_pct_column_renders_with_a_percent_sign() -> None:
    out = render("Late share {{late_share_pct}}, growth {{growth_pct}}.", PCT_ROW)
    assert out.ok
    assert out.text == "Late share 8.11%, growth 140.35%."


def test_pct_column_with_a_sign_or_the_word_in_the_template_is_not_doubled() -> None:
    assert render("{{late_share_pct}}%", PCT_ROW).text == "8.11%"
    assert render("{{late_share_pct}} %", PCT_ROW).text == "8.11%"
    assert render("{{late_share_pct}} percent of orders", PCT_ROW).text == "8.11 percent of orders"
    assert render("{{rows[0].growth_pct}}%", PCT_ROW).text == "140.35%"
    assert render("{{missing_pct}}", PCT_ROW).text == "n/a"


def test_a_fraction_shown_as_a_percentage_fails() -> None:
    out = render("{{late_share}}% of orders were late.", ONE_ROW)
    assert out.text == "{{late_share}}% of orders were late."
    assert len(out.failures) == 1 and "_pct" in out.failures[0] and "late_share" in out.failures[0]
    assert not render("{{late_share}} percent were late.", ONE_ROW).ok
    assert render("{{late_share}} percentile", ONE_ROW).failures == []  # "percentile" is not a unit


def test_years_and_counts_render_as_before_next_to_pct() -> None:
    out = render("In {{year}} there were {{n}} orders, {{late_share_pct}} late.", PCT_ROW)
    assert out.text == "In 2017 there were 45,101 orders, 8.11% late."


def test_money_columns_render_in_reais() -> None:
    out = render("Revenue was {{total_revenue}} from {{n_orders}} orders.", [{"total_revenue": 15739137.01, "n_orders": 98207}])
    assert out.ok
    assert out.text == "Revenue was R$ 15,739,137.01 from 98,207 orders."


def test_state_code_shows_the_state_name():
    from analyst.grounding import render

    out = render("Most unhappy customers live in {{customer_state}}.", [{"customer_state": "SP"}], "")
    assert out.ok and out.text == "Most unhappy customers live in Sao Paulo (SP)."
