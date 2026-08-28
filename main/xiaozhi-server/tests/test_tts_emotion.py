import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.utils.tts_emotion import (
    EmotionStreamParser,
    build_emotion_context,
    compose_style_prompt,
    normalize_emotion_styles,
)


class EmotionStreamParserTests(unittest.TestCase):
    def test_multiple_emotions_across_chunks(self):
        parser = EmotionStreamParser()

        events = []
        for chunk in ["🙂欢迎来到展厅。🤔请仔", "细看。😲这里有惊喜！"]:
            events.extend(parser.feed(chunk))

        contexts = [event.context for event in events if event.context]
        text = "".join(event.text for event in events)
        self.assertEqual([context.emoji for context in contexts], ["🙂", "🤔", "😲"])
        self.assertEqual(text, "欢迎来到展厅。请仔细看。这里有惊喜！")

    def test_default_emotion_and_change_limit(self):
        parser = EmotionStreamParser()

        events = parser.feed("没有显式表情。🤔思考。🤔继续。😲惊讶。😠忽略。")

        contexts = [event.context for event in events if event.context]
        self.assertEqual([context.emoji for context in contexts], ["🙂", "🤔", "😲"])
        self.assertNotIn("😠", "".join(event.text for event in events))

    def test_disabled_parser_strips_all_emoji_without_context(self):
        parser = EmotionStreamParser(enabled=False)

        events = parser.feed("🙂你好🤔世界")

        self.assertFalse(any(event.context for event in events))
        self.assertEqual("".join(event.text for event in events), "你好世界")

class EmotionStylePromptTests(unittest.TestCase):
    def test_dynamic_style_combines_base_and_fixed_profile(self):
        style = compose_style_prompt(
            "声音专业、清晰。",
            build_emotion_context("🤔"),
            True,
        )

        self.assertIn("声音专业、清晰。", style)
        self.assertIn("若有所思", style)

    def test_disabled_dynamic_style_preserves_base_style(self):
        style = compose_style_prompt(
            "声音专业、清晰。",
            build_emotion_context("🤔"),
            False,
        )

        self.assertEqual(style, "声音专业、清晰。")

    def test_custom_profile_style_overrides_builtin_description(self):
        style = compose_style_prompt(
            "年轻女生声线。",
            build_emotion_context("😆"),
            True,
            {"joy": "声音轻快，带明显笑意。"},
        )

        self.assertEqual(
            style,
            "年轻女生声线。 当前片段的表达方式：声音轻快，带明显笑意。",
        )

    def test_emoji_style_takes_priority_over_profile_style(self):
        style = compose_style_prompt(
            "",
            build_emotion_context("😆"),
            True,
            {
                "joy": "通用开心语气。",
                "😆": "笑意更明显，节奏更活泼。",
            },
        )

        self.assertEqual(
            style,
            "当前片段的表达方式：笑意更明显，节奏更活泼。",
        )

    def test_json_string_emotion_styles_are_supported(self):
        styles = normalize_emotion_styles(
            '{"sad":"声音温和低沉。","😆":"声音轻快。"}'
        )

        self.assertEqual(styles["sad"], "声音温和低沉。")
        self.assertEqual(styles["😆"], "声音轻快。")


if __name__ == "__main__":
    unittest.main()
