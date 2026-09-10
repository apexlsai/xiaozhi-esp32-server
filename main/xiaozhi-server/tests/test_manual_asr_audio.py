import queue
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.utils.asr_audio import accept_pcm_frame


class ManualAsrAudioTests(unittest.TestCase):
    def connection(self, mode):
        conn = type("Connection", (), {})()
        conn.client_listen_mode = mode
        conn.asr_audio = []
        conn.asr_audio_queue = queue.Queue()
        return conn

    def test_manual_audio_is_immediately_available_to_listen_stop(self):
        conn = self.connection("manual")

        accept_pcm_frame(conn, b"first")
        accept_pcm_frame(conn, b"second")

        self.assertEqual(conn.asr_audio, [b"first", b"second"])
        self.assertTrue(conn.asr_audio_queue.empty())

    def test_non_manual_audio_keeps_vad_queue(self):
        conn = self.connection("auto")

        accept_pcm_frame(conn, b"frame")

        self.assertEqual(conn.asr_audio, [])
        self.assertEqual(conn.asr_audio_queue.get_nowait(), b"frame")


if __name__ == "__main__":
    unittest.main()
