import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import collect


PODCAST_XML = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel><title>Audio Fixture</title>
    <item>
      <title>Latest episode</title>
      <link>https://podcast.example/episodes/latest</link>
      <pubDate>Mon, 02 Nov 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://cdn.example/latest.mp3" type="audio/mpeg" />
      <itunes:duration>00:09:02</itunes:duration>
    </item>
  </channel>
</rss>"""


class CollectAudioTests(unittest.TestCase):
    def test_sixty_minute_plan_has_eight_chunks_and_one_second_overlap(self):
        plans = collect._plan_audio_chunks(60 * 60)

        self.assertEqual(len(plans), 8)
        self.assertEqual(
            [plan["start_seconds"] for plan in plans],
            [0, 479, 959, 1439, 1919, 2399, 2879, 3359],
        )
        for previous, current in zip(plans, plans[1:]):
            self.assertEqual(current["start_seconds"], previous["end_seconds"] - 1)
        self.assertEqual(plans[-1]["end_seconds"], 60 * 60)
        self.assertTrue(all(plan["end_seconds"] <= 60 * 60 for plan in plans))

    def test_merge_keeps_chunk_order_offsets_and_removes_overlap_text(self):
        transcript = collect._merge_chunk_transcripts([
            ("The opening phrase overlaps here.", 0),
            ("overlaps here. Then the middle phrase.", 479),
            ("middle phrase. Finally done.", 959),
        ])

        self.assertEqual(
            transcript,
            "[00:00] The opening phrase overlaps here.\n"
            "[07:59] Then the middle phrase.\n"
            "[15:59] Finally done.",
        )
        self.assertEqual(transcript.count("overlaps here."), 1)
        self.assertEqual(transcript.count("middle phrase."), 1)

    def test_failed_chunk_returns_partial_result_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp3"
            source.write_bytes(b"source")

            def fake_cut(_source, destination, *_args):
                destination.write_bytes(b"chunk")

            with (
                patch.object(collect, "_detect_silence_points", return_value=[]),
                patch.object(collect, "_cut_audio_chunk", side_effect=fake_cut),
                patch.object(collect, "_groq_transcribe", side_effect=RuntimeError("Groq unavailable")) as groq,
            ):
                result = collect._transcribe_audio_chunks(
                    source, 60, "test-key", time.monotonic() + 5
                )

        self.assertIsNone(result["transcript"])
        self.assertEqual(result["chunk_count"], 1)
        self.assertIs(result["partial"], True)
        self.assertIs(result["timed_out"], False)
        self.assertEqual(groq.call_count, 2)
        self.assertIn("Groq unavailable", result["error"])

    def test_podcast_rss_parser_reads_enclosure_and_duration(self):
        episodes = collect._parse_podcast_feed(PODCAST_XML)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["title"], "Latest episode")
        self.assertEqual(episodes[0]["audio_url"], "https://cdn.example/latest.mp3")
        self.assertEqual(episodes[0]["duration_seconds"], 9 * 60 + 2)
        self.assertEqual(episodes[0]["published"], "2026-11-02T10:00:00Z")

    def test_audio_url_detection_accepts_extension_or_audio_mime(self):
        self.assertTrue(collect._is_audio_url("https://cdn.example/episode.mp3"))
        self.assertTrue(collect._is_audio_url("https://cdn.example/download", "audio/mpeg"))
        self.assertFalse(collect._is_audio_url("https://example.test/episode.html"))


if __name__ == "__main__":
    unittest.main()
