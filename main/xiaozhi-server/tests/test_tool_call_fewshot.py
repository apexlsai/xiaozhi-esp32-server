import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.connection import ConnectionHandler
from core.utils.dialogue import Dialogue


class ToolCallFewshotTests(unittest.TestCase):
    def build_connection(self, enabled=None):
        conn = ConnectionHandler.__new__(ConnectionHandler)
        conn.config = {}
        if enabled is not None:
            conn.config["tool_call_fewshot_enabled"] = enabled
        conn.intent_type = "function_call"
        conn.func_handler = SimpleNamespace(
            get_functions=lambda: [
                {"function": {"name": "handle_exit_intent"}}
            ]
        )
        conn.dialogue = Dialogue()
        conn.logger = MagicMock()
        return conn

    def test_fewshot_is_disabled_by_default(self):
        conn = self.build_connection()

        conn._inject_tool_call_fewshot()

        self.assertEqual(conn.dialogue.dialogue, [])

    def test_fewshot_can_be_enabled_for_legacy_small_models(self):
        conn = self.build_connection("true")

        conn._inject_tool_call_fewshot()

        self.assertEqual(len(conn.dialogue.dialogue), 7)
        self.assertTrue(
            all(message.is_temporary for message in conn.dialogue.dialogue)
        )


if __name__ == "__main__":
    unittest.main()
