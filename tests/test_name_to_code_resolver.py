# -*- coding: utf-8 -*-
"""Tests for src.services.name_to_code_resolver."""

from unittest.mock import patch

from src.services.name_to_code_resolver import (
    _build_reverse_map_no_duplicates,
    _is_code_like,
    _normalize_code,
    _parse_ai_resolution_payload,
    resolve_name_to_code,
    resolve_stock_input,
    resolve_stock_inputs,
)


class TestIsCodeLike:
    def test_a_share_5_digits(self):
        assert _is_code_like("60051") is True
        assert _is_code_like("600519") is True

    def test_a_share_6_digits(self):
        assert _is_code_like("300750") is True

    def test_hk_5_digits(self):
        assert _is_code_like("00700") is True

    def test_us_stock_letters(self):
        assert _is_code_like("AAPL") is True
        assert _is_code_like("TSLA") is True
        assert _is_code_like("BRK.B") is True

    def test_rejects_non_code(self):
        assert _is_code_like("贵州茅台") is False
        assert _is_code_like("1234") is False
        assert _is_code_like("1234567") is False
        assert _is_code_like("") is False
        assert _is_code_like("   ") is False


class TestNormalizeCode:
    def test_preserves_valid_a_share(self):
        assert _normalize_code("600519") == "600519"
        assert _normalize_code("  600519  ") == "600519"

    def test_strips_suffix(self):
        assert _normalize_code("600519.SH") == "600519"
        assert _normalize_code("000001.SZ") == "000001"
        assert _normalize_code("700.HK") == "00700"

    def test_preserves_us_stock(self):
        assert _normalize_code("AAPL") == "AAPL"
        assert _normalize_code("brk.b") == "BRK.B"

    def test_returns_none_for_invalid(self):
        assert _normalize_code("") is None
        assert _normalize_code("1234") is None
        assert _normalize_code("贵州茅台") is None


class TestBuildReverseMapNoDuplicates:
    def test_excludes_ambiguous_names(self):
        code_to_name = {"BABA": "阿里巴巴", "09988": "阿里巴巴", "600519": "贵州茅台"}
        result = _build_reverse_map_no_duplicates(code_to_name)
        assert "阿里巴巴" not in result
        assert result.get("贵州茅台") == "600519"

    def test_includes_unique_names(self):
        code_to_name = {"600519": "贵州茅台", "00700": "腾讯控股"}
        result = _build_reverse_map_no_duplicates(code_to_name)
        assert result["贵州茅台"] == "600519"
        assert result["腾讯控股"] == "00700"


class TestResolveNameToCode:
    def test_code_like_input_returned_normalized(self):
        assert resolve_name_to_code("600519") == "600519"
        assert resolve_name_to_code("600519.SH") == "600519"
        assert resolve_name_to_code("  AAPL  ") == "AAPL"

    def test_local_map_exact_match(self):
        assert resolve_name_to_code("贵州茅台") == "600519"
        assert resolve_name_to_code("腾讯控股") == "00700"

    def test_local_alias_match_for_foreign_company_name(self):
        assert resolve_name_to_code("Klarna") == "KLAR"
        assert resolve_name_to_code("Klarna Group plc") == "KLAR"
        assert resolve_stock_input("klarna") == "KLAR"

    @patch("src.services.name_to_code_resolver._call_ai_name_resolver")
    def test_deterministic_match_does_not_invoke_ai_fallback(self, mock_ai):
        assert resolve_name_to_code("Klarna") == "KLAR"
        mock_ai.assert_not_called()

    def test_returns_none_for_empty_or_invalid_input(self):
        assert resolve_name_to_code("") is None
        assert resolve_name_to_code("   ") is None
        assert resolve_name_to_code(None) is None  # type: ignore[arg-type]

    @patch("src.services.name_to_code_resolver._call_ai_name_resolver", return_value=None)
    def test_ambiguous_name_returns_none(self, _mock_ai):
        assert resolve_name_to_code("阿里巴巴") is None

    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code")
    def test_akshare_fallback_when_not_in_local(self, mock_akshare):
        mock_akshare.return_value = {"浦发银行": "600000"}
        result = resolve_name_to_code("浦发银行")
        assert result == "600000"
        mock_akshare.assert_called()

    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code")
    def test_fuzzy_match_fallback(self, mock_akshare):
        mock_akshare.return_value = {"贵州茅台": "600519"}
        result = resolve_name_to_code("贵州茅苔")
        assert result == "600519"

    @patch("src.services.name_to_code_resolver._call_ai_name_resolver", return_value=None)
    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code")
    def test_returns_none_when_no_match(self, mock_akshare, _mock_ai):
        mock_akshare.return_value = {}
        result = resolve_name_to_code("不存在的股票名称xyz")
        assert result is None

    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code")
    def test_yfinance_search_fallback_for_foreign_name(self, mock_akshare):
        mock_akshare.return_value = {}

        class SearchStub:
            def __init__(self, *args, **kwargs):
                self.quotes = [
                    {
                        "symbol": "NU",
                        "quoteType": "EQUITY",
                        "shortname": "Nu Holdings Ltd.",
                        "longname": "Nu Holdings Ltd.",
                    }
                ]

        with patch("yfinance.Search", SearchStub):
            result = resolve_name_to_code("Nu Holdings")

        assert result == "NU"

    @patch("src.services.name_to_code_resolver._call_ai_name_resolver", return_value=None)
    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code")
    def test_resolve_stock_inputs_collects_unresolved_values(self, mock_akshare, _mock_ai):
        mock_akshare.return_value = {}

        class SearchStub:
            def __init__(self, *args, **kwargs):
                self.quotes = []

        with patch("yfinance.Search", SearchStub):
            resolved, unresolved = resolve_stock_inputs(["Klarna", "AAPL", "   ", "DefinitelyUnknownXYZ"])

        assert resolved == ["KLAR", "AAPL"]
        assert unresolved == ["DefinitelyUnknownXYZ"]

    @patch("src.services.name_to_code_resolver._search_foreign_name_to_code", return_value=None)
    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code", return_value={})
    @patch(
        "src.services.name_to_code_resolver._call_ai_name_resolver",
        return_value={"ticker": "AAPL", "canonical_name": "Apple Inc.", "confidence": 0.98},
    )
    def test_ai_fallback_can_resolve_direct_ticker(self, _mock_ai, _mock_akshare, _mock_yf):
        assert resolve_name_to_code("苹果公司") == "AAPL"

    @patch("src.services.name_to_code_resolver._search_foreign_name_to_code", return_value=None)
    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code", return_value={})
    @patch(
        "src.services.name_to_code_resolver._call_ai_name_resolver",
        return_value={"canonical_name": "Klarna Group plc", "ticker": None, "confidence": 0.91},
    )
    def test_ai_fallback_can_resolve_via_canonical_name(self, _mock_ai, _mock_akshare, _mock_yf):
        assert resolve_name_to_code("klarna支付公司") == "KLAR"

    @patch("src.services.name_to_code_resolver._search_foreign_name_to_code", return_value=None)
    @patch("src.services.name_to_code_resolver._get_akshare_name_to_code", return_value={})
    @patch(
        "src.services.name_to_code_resolver._call_ai_name_resolver",
        return_value={"ticker": "ZZZZ", "canonical_name": "", "confidence": 0.2},
    )
    def test_ai_fallback_rejects_low_confidence_direct_code(self, _mock_ai, _mock_akshare, _mock_yf):
        assert resolve_name_to_code("某个非常模糊的公司") is None


class TestAiPayloadParsing:
    def test_parses_plain_json(self):
        payload = _parse_ai_resolution_payload('{"ticker":"AAPL","canonical_name":"Apple Inc.","confidence":0.9}')
        assert payload["ticker"] == "AAPL"

    def test_parses_fenced_json(self):
        payload = _parse_ai_resolution_payload(
            '```json\n{"ticker":"KLAR","canonical_name":"Klarna Group plc","confidence":"high"}\n```'
        )
        assert payload["ticker"] == "KLAR"
