from decimal import Decimal

from legal_funds_agent.services.chinese_numerals import parse_chinese_numeral
from legal_funds_agent.services.extraction_guard import check_amount_anchor
from legal_funds_agent.services.case_narrative_service import _numeric_tokens


def test_uppercase_chinese_numerals():
    assert parse_chinese_numeral("壹佰贰拾伍万元") == Decimal("1250000")
    assert parse_chinese_numeral("伍万元整") == Decimal("50000")
    assert parse_chinese_numeral("壹拾万元") == Decimal("100000")


def test_lowercase_chinese_numerals():
    assert parse_chinese_numeral("一百二十五万") == Decimal("1250000")
    assert parse_chinese_numeral("五万元") == Decimal("50000")
    assert parse_chinese_numeral("十") == Decimal("10")


def test_prefix_and_suffix_tolerance():
    assert parse_chinese_numeral("人民币壹佰贰拾伍万元") == Decimal("1250000")
    assert parse_chinese_numeral("伍万圆整") == Decimal("50000")


def test_invalid_input_returns_none():
    assert parse_chinese_numeral("") is None
    assert parse_chinese_numeral("abc") is None
    assert parse_chinese_numeral("一二点五") is None


def test_uppercase_amount_is_anchored_in_source():
    assert check_amount_anchor(Decimal("1250000.00"), "骗取人民币壹佰贰拾伍万元") is None


def test_numeric_tokens_include_chinese_numerals():
    tokens = _numeric_tokens("已确证壹佰贰拾伍万元，另有 50,000.00 元。")
    assert Decimal("1250000") in tokens
    assert Decimal("50000") in tokens
