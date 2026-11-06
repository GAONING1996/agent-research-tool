"""Requirements must treat Tavily and SearXNG as alternatives, not both mandatory."""

import unittest

import research


def row(key, tier, state, asked="?"):
    return {"key": key, "tier": tier, "state": state, "asked_as": asked,
            "how_to_get": "get it", "missing_effect": "less coverage"}


class RequirementTests(unittest.TestCase):
    def test_searxng_ready_does_not_require_tavily(self):
        rows = [
            row("TAVILY_API_KEY", "required", "missing"),
            row("SEARXNG_URL", "required", "ready"),
            row("GROQ_API_KEY", "user_key", "missing"),
        ]
        requirements, needs_user = research._human_requirements(rows)
        keys = [item["key"] for item in requirements]
        self.assertNotIn("TAVILY_API_KEY", keys)
        self.assertNotIn("SEARCH_PROVIDER", keys)
        self.assertIn("GROQ_API_KEY", keys)
        self.assertTrue(needs_user)

    def test_tavily_ready_does_not_require_searxng(self):
        rows = [
            row("TAVILY_API_KEY", "required", "ready"),
            row("SEARXNG_URL", "required", "missing"),
        ]
        requirements, needs_user = research._human_requirements(rows)
        self.assertEqual(requirements, [])
        self.assertFalse(needs_user)

    def test_both_missing_collapse_into_one_choice(self):
        rows = [
            row("TAVILY_API_KEY", "required", "missing"),
            row("SEARXNG_URL", "required", "missing"),
        ]
        requirements, needs_user = research._human_requirements(rows)
        self.assertEqual([item["key"] for item in requirements], ["SEARCH_PROVIDER"])
        self.assertTrue(requirements[0]["asked_as"].startswith("需要你给我一个搜索 key"))
        self.assertTrue(needs_user)

    def test_declined_rows_are_not_requirements(self):
        rows = [
            row("TAVILY_API_KEY", "required", "declined"),
            row("SEARXNG_URL", "required", "ready"),
        ]
        requirements, needs_user = research._human_requirements(rows)
        self.assertEqual(requirements, [])
        self.assertFalse(needs_user)


if __name__ == "__main__":
    unittest.main()
