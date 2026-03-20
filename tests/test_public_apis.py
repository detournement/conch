"""Tests for conch.public_apis — catalog parsing, search, and API calling."""

import json
import unittest
from unittest.mock import patch, MagicMock

from conch.public_apis import _parse_readme, search, call_api, get_categories


SAMPLE_README = """
# Public APIs

## Index
* [Animals](#animals)
* [Weather](#weather)

### Animals
API | Description | Auth | HTTPS | CORS
|:---|:---|:---|:---|:---|
| [Cat Facts](https://alexwohlbruck.github.io/cat-facts/) | Daily cat facts | No | Yes | No |
| [Dogs](https://dog.ceo/dog-api/) | Based on the Stanford Dogs Dataset | No | Yes | Yes |
| [Cats](https://docs.thecatapi.com/) | Pictures of cats from Tumblr | `apiKey` | Yes | No |
| [IUCN](http://apiv3.iucnredlist.org/api/v3/docs) | IUCN Red List of Threatened Species | `apiKey` | No | No |

### Weather
API | Description | Auth | HTTPS | CORS
|:---|:---|:---|:---|:---|
| [Open-Meteo](https://open-meteo.com/) | Global weather forecast API for non-commercial use | No | Yes | Yes |
| [OpenWeatherMap](https://openweathermap.org/api) | Weather | `apiKey` | Yes | Unknown |
| [7Timer!](http://www.7timer.info/doc.php?lang=en) | Weather, especially for Astroweather | No | No | Unknown |
"""


class TestParseReadme(unittest.TestCase):
    def test_parses_entries(self):
        entries = _parse_readme(SAMPLE_README)
        self.assertGreater(len(entries), 0)

    def test_correct_count(self):
        entries = _parse_readme(SAMPLE_README)
        self.assertEqual(len(entries), 7)

    def test_entry_fields(self):
        entries = _parse_readme(SAMPLE_README)
        cat_facts = next(e for e in entries if e["name"] == "Cat Facts")
        self.assertEqual(cat_facts["url"], "https://alexwohlbruck.github.io/cat-facts/")
        self.assertEqual(cat_facts["description"], "Daily cat facts")
        self.assertEqual(cat_facts["auth"], "none")
        self.assertTrue(cat_facts["https"])
        self.assertEqual(cat_facts["category"], "Animals")

    def test_api_key_auth(self):
        entries = _parse_readme(SAMPLE_README)
        cats = next(e for e in entries if e["name"] == "Cats")
        self.assertEqual(cats["auth"], "apiKey")

    def test_http_not_https(self):
        entries = _parse_readme(SAMPLE_README)
        iucn = next(e for e in entries if e["name"] == "IUCN")
        self.assertFalse(iucn["https"])

    def test_categories(self):
        entries = _parse_readme(SAMPLE_README)
        categories = set(e["category"] for e in entries)
        self.assertIn("Animals", categories)
        self.assertIn("Weather", categories)


class TestSearch(unittest.TestCase):
    @patch("conch.public_apis.load_catalog")
    def test_keyword_search(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("cat")
        names = [r["name"] for r in results]
        self.assertIn("Cat Facts", names)
        self.assertIn("Cats", names)

    @patch("conch.public_apis.load_catalog")
    def test_no_auth_filter(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("", auth_filter="none")
        for r in results:
            self.assertEqual(r["auth"], "none")

    @patch("conch.public_apis.load_catalog")
    def test_category_filter(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("", category_filter="Weather")
        for r in results:
            self.assertEqual(r["category"], "Weather")

    @patch("conch.public_apis.load_catalog")
    def test_empty_query_returns_all(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("")
        self.assertEqual(len(results), 7)

    @patch("conch.public_apis.load_catalog")
    def test_no_match(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("xyznonexistent")
        self.assertEqual(len(results), 0)

    @patch("conch.public_apis.load_catalog")
    def test_limit(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        results = search("", limit=3)
        self.assertEqual(len(results), 3)


class TestGetCategories(unittest.TestCase):
    @patch("conch.public_apis.load_catalog")
    def test_returns_counts(self, mock_load):
        mock_load.return_value = _parse_readme(SAMPLE_README)
        cats = get_categories()
        self.assertEqual(cats["Animals"], 4)
        self.assertEqual(cats["Weather"], 3)


class TestCallApi(unittest.TestCase):
    def test_empty_url(self):
        result = call_api("")
        self.assertIn("Error", result)

    def test_invalid_scheme(self):
        result = call_api("ftp://example.com")
        self.assertIn("Error", result)

    @patch("conch.public_apis.urllib.request.urlopen")
    def test_successful_call(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"fact": "Cats sleep 16 hours a day"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = call_api("https://example.com/api")
        self.assertIn("Cats sleep", result)

    @patch("conch.public_apis.urllib.request.urlopen")
    def test_truncation(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"x" * 9000
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = call_api("https://example.com/api")
        self.assertIn("truncated", result)

    def test_network_error(self):
        result = call_api("https://this-domain-does-not-exist-12345.example.com/api")
        self.assertIn("failed", result.lower())


if __name__ == "__main__":
    unittest.main()
