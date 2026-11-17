from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch

import outages
import telegram_bot as bot


class OutageTests(unittest.TestCase):
    def test_no_street_returns_hint_without_fetching_first_five_streets(self):
        with patch.object(outages, "api_collection") as fetch:
            text = outages.current_message(datetime.now(timezone.utc))
        fetch.assert_not_called()
        self.assertIn("Відключення: Михайла Грушевського", text)
        self.assertIn(outages.CURRENT_URL, text)
        self.assertNotIn("Показано", text)

    def test_button_keeps_schedule_and_address_hint(self):
        api = Mock()
        with patch.object(bot, "schedule_messages", return_value=["schedule"]), patch.object(outages, "api_collection") as fetch:
            bot.handle_update(api, {"update_id": 1, "message": {"chat": {"id": 123, "type": "private"}, "text": "Відключення"}}, None, {}, "model")
        fetch.assert_not_called()
        self.assertEqual(api.send.call_count, 2)
        self.assertEqual(api.send.call_args_list[0].args[1], "schedule")
        self.assertIn("Відключення:", api.send.call_args.args[1])

    def test_named_street_still_filters_live_outages(self):
        row = {"city": {"id": outages.ZHYTOMYR_ID}, "street": {"id": 5126, "name": "Михайла Грушевського"},
               "buildingNames": "16", "typeOff": 1}
        with patch.object(outages, "street_matches", return_value=[row["street"]]), patch.object(outages, "api_collection", return_value=([row], 1)) as fetch:
            text = outages.current_message(datetime.now(timezone.utc), "Михайла Грушевського")
        self.assertEqual(fetch.call_args.kwargs["street.id"], 5126)
        self.assertIn("Будинки: 16", text)


if __name__ == "__main__":
    unittest.main()
