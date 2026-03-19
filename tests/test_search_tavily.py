# -*- coding: utf-8 -*-
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np


from src.search_service import TavilySearchProvider


class _FakeTavilyClient:
    last_kwargs = None

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, **kwargs):
        _FakeTavilyClient.last_kwargs = kwargs
        return {
            "results": [
                {
                    "title": "Klarna expands partnership",
                    "content": "Latest coverage",
                    "url": "https://example.com/klarna-news",
                    "date": "2026-03-19",
                }
            ]
        }


class TavilySearchProviderTestCase(unittest.TestCase):
    def test_infer_topic_for_news_query(self) -> None:
        self.assertEqual(
            TavilySearchProvider._infer_topic("Klarna KLAR stock latest news"),
            "news",
        )
        self.assertIsNone(TavilySearchProvider._infer_topic("Klarna company overview"))

    def test_search_uses_news_topic_and_fallback_date_field(self) -> None:
        provider = TavilySearchProvider(["dummy-key"])
        fake_module = types.SimpleNamespace(TavilyClient=_FakeTavilyClient)

        with patch.dict(sys.modules, {"tavily": fake_module}):
            response = provider.search("Klarna KLAR stock latest news", max_results=5, days=3)

        self.assertTrue(response.success)
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].published_date, "2026-03-19")
        self.assertEqual(_FakeTavilyClient.last_kwargs["topic"], "news")
        self.assertEqual(_FakeTavilyClient.last_kwargs["days"], 3)


if __name__ == "__main__":
    unittest.main()
