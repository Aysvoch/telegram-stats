import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

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


class FakeMessage:
    def __init__(self, message_id, text=None, media=True, grouped_id=None,
                 views=10):
        self.id = message_id
        self.message = text
        self.media = media
        self.grouped_id = grouped_id
        self.views = views
        self.forwards = 0
        self.replies = None
        self.reactions = None
        self.date = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)


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

    def test_google_auth_uses_only_sheets_scope(self):
        settings = stats.Settings(
            api_id=1,
            api_hash="hash",
            channel="channel",
            spreadsheet_id="sheet-id",
            service_account_file=None,
            post_fetch_limit=None,
        )
        fake_client = Mock()
        fake_client.open_by_key.return_value = "book"
        with patch.dict(
                os.environ, {"GOOGLE_CREDENTIALS": "{}"}, clear=True):
            with patch.object(
                    stats.Credentials, "from_service_account_info",
                    return_value="credentials") as credentials_mock:
                with patch.object(
                        stats.gspread, "authorize",
                        return_value=fake_client):
                    result = stats.get_book(settings)

        self.assertEqual(result, "book")
        scopes = credentials_mock.call_args.kwargs["scopes"]
        self.assertEqual(
            scopes, ["https://www.googleapis.com/auth/spreadsheets"])

    def test_err_includes_all_interactions(self):
        self.assertEqual(stats.engagement_rate(10, 3, 2, 100), 15.0)
        self.assertEqual(stats.engagement_rate(10, 3, 2, 0), 0)

    def test_err_rounding_matches_google_sheets(self):
        self.assertEqual(stats.engagement_rate(1, 0, 0, 80), 1.3)

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

    def test_media_album_is_one_post_and_standalone_media_is_kept(self):
        posts = stats.collapse_telegram_messages([
            FakeMessage(8, grouped_id=100, views=120),
            FakeMessage(7, text="подпись альбома", grouped_id=100, views=100),
            FakeMessage(6, views=50),
            FakeMessage(5, media=False),
        ])

        self.assertEqual([item["id"] for item in posts], [7, 6])
        self.assertEqual(posts[0]["component_ids"], [7, 8])
        self.assertEqual(posts[0]["views"], 100)
        self.assertEqual(posts[1]["text_preview"], "[медиа без подписи]")

    def test_album_cleanup_migrates_manual_history(self):
        existing = [
            stats.POST_HEADER,
            [8, "2026-01-01 10:00", "url8", "[медиа без подписи]",
             120, "", 95, 0, "—", "", "", "", "заметка 2", 0, 0, ""],
            [7, "2026-01-01 10:00", "url7", "подпись", 100, 80, "",
             4, "👍 4", 50, "", "", "заметка 1", 1, 2, ""],
        ]
        fresh = [post(7, "2026-01-01 10:00", 110, reactions=5)]
        fresh[0]["component_ids"] = [7, 8]

        rows, analytics = stats.build_post_rows(
            existing, fresh, 60, "@channel",
            now=datetime(2026, 1, 4, 12, tzinfo=timezone.utc))

        self.assertEqual([row[0] for row in rows[1:]], [7])
        self.assertEqual(rows[1][5], 80)
        self.assertEqual(rows[1][6], 95)
        self.assertEqual(rows[1][9], 50)
        self.assertEqual(rows[1][12], "заметка 1\nзаметка 2")
        self.assertEqual([item["id"] for item in analytics], [7])

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


def slice_post(post_id, date, views, *, views_24=None, views_72=None,
               captured_targets=None, is_fresh=True):
    return {
        "id": post_id,
        "date": date,
        "url": f"https://t.me/channel/{post_id}",
        "views": views,
        "views_24": views_24,
        "views_72": views_72,
        "reactions_total": 3,
        "forwards": 1,
        "replies": 1,
        "subscribers_at_post": 50,
        "captured_targets": set(captured_targets or ()),
        "is_fresh": is_fresh,
    }


class SliceLogicTests(unittest.TestCase):
    def test_slice_header_tolerates_spacing_differences(self):
        header = [f"  {value.replace(' ', '  ')}  "
                  for value in stats.SLICE_HEADER]
        self.assertTrue(stats._slice_header_matches([header]))

    def test_conflicting_slice_sheet_is_renamed_not_deleted(self):
        class Worksheet:
            def __init__(self, title):
                self.title = title

            def update_title(self, title):
                self.title = title

        class Book:
            def __init__(self, worksheet):
                self.old = worksheet
                self.new = Worksheet("new")
                self.added = None

            def worksheets(self):
                return [self.old]

            def add_worksheet(self, **kwargs):
                self.added = kwargs
                self.new.title = kwargs["title"]
                return self.new

        old = Worksheet("Срезы")
        book = Book(old)
        replacement = stats._replace_conflicting_slice_sheet(
            old, book,
            now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))

        self.assertEqual(old.title, "Срезы — резерв 20260102-030405")
        self.assertEqual(replacement.title, "Срезы")
        self.assertEqual(book.added, {"title": "Срезы", "rows": 500, "cols": 20})

    def test_live_and_legacy_slices_have_honest_metadata(self):
        now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        posts = [
            slice_post(1, "2026-01-01 12:00", 130,
                       views_24=80, views_72=100, is_fresh=False),
            slice_post(2, "2026-01-10 02:00", 30),
        ]

        rows, counts = stats.build_slice_rows(
            [stats.SLICE_HEADER], posts, 60, now=now)
        by_key = {(row[0], row[3]): row for row in rows}

        self.assertEqual(counts, {"legacy_posts": 2, "live": 1})
        self.assertEqual(set(by_key), {(1, 24), (1, 72), (2, 6)})

        legacy_24 = by_key[(1, 24)]
        self.assertIsNone(legacy_24[4])
        self.assertIsNone(legacy_24[5])
        self.assertIsNone(legacy_24[10])
        self.assertIsNone(legacy_24[13])
        self.assertEqual(legacy_24[17], "legacy_posts")

        legacy_72 = by_key[(1, 72)]
        self.assertEqual(legacy_72[7], 20)
        self.assertEqual(legacy_72[8], 25.0)

        live_6 = by_key[(2, 6)]
        self.assertEqual(live_6[4], 10.0)
        self.assertEqual(live_6[5], now)
        self.assertEqual(live_6[9], 3.0)
        self.assertEqual(live_6[13], 16.7)
        self.assertEqual(live_6[15], 60)
        self.assertEqual(live_6[16], 60.0)
        self.assertEqual(live_6[17], "live")

    def test_slice_keys_are_idempotent(self):
        now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        current = slice_post(2, "2026-01-10 02:00", 30)
        existing = [
            stats.SLICE_HEADER,
            [2, "2026-01-10 02:00", "url", 6, 10, "2026-01-10 12:00",
             30, "", "", 3, 3, 1, 1, 16.7, 50, 60, 60, "live"],
        ]

        rows, counts = stats.build_slice_rows(
            existing, [current], 60, now=now)

        self.assertEqual(rows, [])
        self.assertEqual(counts, {})

    def test_duplicate_slice_key_stops_the_write(self):
        duplicate = [
            2, "2026-01-10 02:00", "url", 6, 10,
            "2026-01-10 12:00", 30,
        ]
        with self.assertRaisesRegex(RuntimeError, "повтор ключа"):
            stats.build_slice_rows(
                [stats.SLICE_HEADER, duplicate, duplicate], [], 60)

    def test_new_24_hour_value_is_recorded_as_live(self):
        now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        current = slice_post(
            3, "2026-01-09 06:00", 40, views_24=40,
            captured_targets={24})

        rows, counts = stats.build_slice_rows(
            [stats.SLICE_HEADER], [current], 60, now=now)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 24)
        self.assertEqual(rows[0][4], 30.0)
        self.assertEqual(rows[0][17], "live")
        self.assertEqual(counts, {"live": 1})

    def test_slice_dates_are_numeric_google_cells(self):
        cell = stats.sheets_datetime_cell(
            datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIn("numberValue", cell["userEnteredValue"])
        self.assertEqual(
            cell["userEnteredFormat"]["numberFormat"]["type"],
            "DATE_TIME")

    def test_slice_writer_appends_typed_dates(self):
        class SliceWorksheet:
            id = 10
            row_count = 500
            col_count = 20

            def get_all_values(self):
                return [stats.SLICE_HEADER]

            def resize(self, **_kwargs):
                raise AssertionError("resize is not expected")

        class SliceBook:
            def __init__(self):
                self.calls = []

            def batch_update(self, body):
                self.calls.append(body)

        now = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        book = SliceBook()
        result = stats.write_slices(
            SliceWorksheet(), book,
            [slice_post(2, "2026-01-10 02:00", 30)], 60, now=now)

        append = next(
            request["appendCells"]
            for call in book.calls for request in call["requests"]
            if "appendCells" in request)
        values = append["rows"][0]["values"]
        self.assertEqual(len(values), len(stats.SLICE_HEADER))
        self.assertIn("numberValue", values[1]["userEnteredValue"])
        self.assertIn("numberValue", values[5]["userEnteredValue"])
        self.assertEqual(values[17]["userEnteredValue"]["stringValue"], "live")
        self.assertEqual(result, {"added": 1, "live": 1, "legacy": 0})


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

    def get(self, _address, value_render_option=None):
        if self.title == "Динамика":
            return [[46000.0], ["2026-09-18 07:50"]]
        return []


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
        self.assertIn("numberValue", values[0]["userEnteredValue"])
        self.assertEqual(
            values[0]["userEnteredFormat"]["numberFormat"]["type"],
            "DATE_TIME")
        self.assertEqual(values[2]["userEnteredValue"]["numberValue"], 1)
        self.assertEqual(values[3]["userEnteredValue"]["numberValue"], 1)

        repairs = [request["updateCells"] for request in requests
                   if request.get("updateCells", {}).get("start", {}).get(
                       "sheetId") == 2]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["start"]["rowIndex"], 2)
        repaired_cell = repairs[0]["rows"][0]["values"][0]
        self.assertIn("numberValue", repaired_cell["userEnteredValue"])


if __name__ == "__main__":
    unittest.main()
