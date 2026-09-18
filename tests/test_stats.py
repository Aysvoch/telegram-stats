import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import stats


def post(post_id, date, views, reactions=0, forwards=0, replies=0):
    return {
        "id": post_id,
        "date": date,
        "text_preview": f"post {post_id}",
        "views": views,
        "forwards": forwards,
        "replies": replies,
        "reactions_total": reactions,
        "reactions_fmt": stats.fmt_reactions({"👍": reactions}),
        "err": stats.engagement_rate(reactions, forwards, replies, views),
    }


class StatisticsLogicTests(unittest.TestCase):
    def test_settings_are_loaded_only_when_requested(self):
        environment = {
            "API_ID": "123",
            "API_HASH": "hash",
            "CHANNEL": "channel",
            "SPREADSHEET_ID": "sheet",
            "POST_FETCH_LIMIT": "0",
        }
        with patch.object(stats, "load_dotenv") as load_dotenv_mock:
            with patch.dict(os.environ, environment, clear=True):
                settings = stats.load_settings()
        load_dotenv_mock.assert_called_once_with()
        self.assertEqual(settings.api_id, 123)
        self.assertIsNone(settings.post_fetch_limit)

    def test_err_includes_all_interactions(self):
        self.assertEqual(stats.engagement_rate(10, 3, 2, 100), 15.0)
        self.assertEqual(stats.engagement_rate(10, 3, 2, 0), 0)

    def test_weekly_summary_is_chronological_and_weighted(self):
        posts = [
            post(1, "2026-01-12 10:00", 100, reactions=10),
            post(2, "2026-01-05 10:00", 900),
            post(3, "2026-01-05 11:00", 100, reactions=10),
        ]
        result = stats.weekly_summary(posts)
        self.assertTrue(result[0][0].startswith("2026 · Нед.02"))
        self.assertTrue(result[1][0].startswith("2026 · Нед.03"))
        self.assertEqual(result[0][3], 1.0)
        self.assertEqual(result[1][3], 10.0)

    def test_post_merge_preserves_history_and_manual_values(self):
        existing = [
            stats.POST_HEADER,
            [1, "2026-01-01 10:00", "url", "old", 100, 80, 95, 4,
             "👍 4", 50, "", "", "важная заметка", 1, 2, ""],
        ]
        fresh = [post(2, "2026-01-03 10:00", 30, reactions=3)]
        rows, analytics = stats.build_post_rows(
            existing, fresh, 60, "@channel",
            now=datetime(2026, 1, 3, 12, tzinfo=timezone.utc))
        self.assertEqual([row[0] for row in rows[1:]], [2, 1])
        self.assertEqual(rows[2][5], 80)
        self.assertEqual(rows[2][6], 95)
        self.assertEqual(rows[2][9], 50)
        self.assertEqual(rows[2][12], "важная заметка")
        self.assertEqual({item["id"] for item in analytics}, {1, 2})

    def test_post_links_support_common_channel_formats(self):
        self.assertEqual(stats.post_url("@name", 5), "https://t.me/name/5")
        self.assertEqual(
            stats.post_url("https://t.me/name", 5), "https://t.me/name/5")
        self.assertEqual(
            stats.post_url("-100123456", 5), "https://t.me/c/123456/5")

    def test_audience_delta_distinguishes_first_empty_snapshot(self):
        self.assertEqual(stats.calculate_audience_delta(set(), {1}, False), ("", ""))
        self.assertEqual(stats.calculate_audience_delta(set(), {1}, True), (1, 0))
        self.assertEqual(stats.calculate_audience_delta({1, 2}, {2, 3}, True), (1, 1))


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeTelegram:
    def __init__(self, ids, fail_after=None):
        self.ids = ids
        self.fail_after = fail_after

    async def iter_participants(self, _channel):
        for index, user_id in enumerate(self.ids):
            if self.fail_after is not None and index == self.fail_after:
                raise RuntimeError("temporary Telegram failure")
            yield FakeUser(user_id)


class SubscriberSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_snapshot_is_discarded(self):
        result = await stats.collect_subscriber_ids(
            FakeTelegram([1, 2, 3], fail_after=2), "channel", 3)
        self.assertIsNone(result)

    async def test_complete_snapshot_is_accepted(self):
        result = await stats.collect_subscriber_ids(
            FakeTelegram([1, 2, 3]), "channel", 3)
        self.assertEqual(result, {1, 2, 3})

    async def test_large_count_mismatch_is_discarded(self):
        result = await stats.collect_subscriber_ids(
            FakeTelegram([1, 2, 3]), "channel", 100)
        self.assertIsNone(result)


class FakeCell:
    def __init__(self, value):
        self.value = value


class FakeWorksheet:
    def __init__(self, title, sheet_id):
        self.title = title
        self.id = sheet_id
        self.row_count = 500
        self.col_count = 20

    def col_values(self, _column):
        return ["1", "2"] if self.title == "_Аудитория" else []

    def acell(self, address):
        if self.title == "_Аудитория" and address == "B1":
            return FakeCell("initialized")
        if self.title == "Динамика" and address == "A1":
            return FakeCell("Дата (UTC)")
        return FakeCell(None)


class FakeBook:
    def __init__(self):
        self.sheets = {
            "_Аудитория": FakeWorksheet("_Аудитория", 1),
            "Динамика": FakeWorksheet("Динамика", 2),
        }
        self.calls = []

    def worksheets(self):
        return list(self.sheets.values())

    def worksheet(self, title):
        return self.sheets[title]

    def batch_update(self, body):
        self.calls.append(body)


class DynamicsWriteTests(unittest.TestCase):
    def test_snapshot_and_history_use_one_atomic_batch(self):
        book = FakeBook()
        stats.write_dynamics(book, {2, 3}, 2)

        self.assertEqual(len(book.calls), 1)
        requests = book.calls[0]["requests"]
        self.assertEqual(requests[0].get("repeatCell", {}).get("fields"),
                         "userEnteredValue")
        append = next(request["appendCells"] for request in requests
                      if "appendCells" in request)
        values = append["rows"][0]["values"]
        self.assertEqual(values[2]["userEnteredValue"]["numberValue"], 1)
        self.assertEqual(values[3]["userEnteredValue"]["numberValue"], 1)


if __name__ == "__main__":
    unittest.main()
