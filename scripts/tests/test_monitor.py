import unittest
import os
import csv
import json
import shutil
from unittest.mock import patch, MagicMock
from twitch_monitor import TwitchMonitor, CSV_COLUMNS

class TestTwitchMonitor(unittest.TestCase):
    def setUp(self):
        # Setup temporary files for testing
        self.test_dir = "test_env"
        os.makedirs(self.test_dir, exist_ok=True)
        self.log_file = os.path.join(self.test_dir, "Signal_Intake_Log.csv")
        self.baseline_file = os.path.join(self.test_dir, "baseline_stats.json")
        self.neg_word_file = os.path.join(self.test_dir, "negword_list.txt")
        
        # Patch the constants in twitch_monitor
        self.patcher_log = patch("twitch_monitor.LOG_FILE", self.log_file)
        self.patcher_baseline = patch("twitch_monitor.BASELINE_FILE", self.baseline_file)
        self.patcher_neg = patch("twitch_monitor.NEG_WORD_FILE", self.neg_word_file)
        self.patcher_log.start()
        self.patcher_baseline.start()
        self.patcher_neg.start()

        with open(self.neg_word_file, "w", encoding="utf-8") as f:
            f.write("nerf, broken, op")

        self.monitor = TwitchMonitor(debug=False)
        self.monitor.set_init_baseline(500)

    def tearDown(self):
        self.patcher_log.stop()
        self.patcher_baseline.stop()
        self.patcher_neg.stop()
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_viewership_spike(self):
        # Case 1: Viewers +40% (High Intensity, Observation)
        # Baseline is 500. 500 * 1.4 = 700.
        with patch.object(self.monitor, "_api_request") as mock_api:
            mock_api.return_value = {
                "data": [{"viewer_count": 700}]
            }
            self.monitor.check_streams("fake_game_id")
            
            # Verify CSV
            self.assertTrue(os.path.exists(self.log_file))
            with open(self.log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["Intensity"], "High")
                self.assertEqual(rows[0]["Signal Type"], "Observation")
                self.assertIn("比+40.0%", rows[0]["Summary"])

    def test_clip_frequency(self):
        # Case 2: Clips > 10 (High Intensity)
        with patch.object(self.monitor, "_api_request") as mock_api:
            # Return 11 clips
            mock_api.return_value = {
                "data": [{"id": str(i)} for i in range(11)]
            }
            self.monitor.check_clips("fake_game_id")
            
            with open(self.log_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["Intensity"], "High")
                self.assertIn("11件", rows[0]["Summary"])

    def test_negative_sentiment(self):
        # Case 3: Neg rate > 25% (High Intensity, Complaint)
        # Mock 100 messages with 30 negative hits
        from twitch_monitor import ChatMonitor
        chat = ChatMonitor(self.monitor)
        chat.buffer = ["nerf this"] * 30 + ["gg"] * 70
        
        chat.analyze_buffer()
        
        with open(self.log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Signal Type"], "Complaint")
            self.assertEqual(rows[0]["Intensity"], "High")
            self.assertIn("30.0%", rows[0]["Summary"])

    def test_cooldown_logic(self):
        # Case 4: Cooldown logic
        # 1. Log a signal
        self.monitor.log_signal(
            signal_type="Observation",
            summary="Test Summary",
            intensity="Low",
            evidence_link="http",
            source_detail="N/A"
        )
        
        # Verify it's logged
        with open(self.log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 1)

        # 2. Try to log another signal of same type immediately
        # Current COOLDOWN_MINUTES is 30.
        self.monitor.log_signal(
            signal_type="Observation",
            summary="Another Summary",
            intensity="Low",
            evidence_link="http",
            source_detail="N/A"
        )

        # Verify it is NOT logged (still expect 1 row)
        with open(self.log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 1)

        # 3. Log a different type (Complaint) - should be allowed
        self.monitor.log_signal(
            signal_type="Complaint",
            summary="Complaint Summary",
            intensity="Low",
            evidence_link="http",
            source_detail="N/A"
        )
        
        with open(self.log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["Signal Type"], "Complaint")

if __name__ == "__main__":
    unittest.main()
