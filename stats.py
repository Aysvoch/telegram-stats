# ============================================================
#  СБОР АНАЛИТИКИ TELEGRAM-КАНАЛА «Приватный эфир»
#  Telethon -> Google Sheets
#
#  Работает в двух режимах (определяется автоматически):
#   - ОБЛАКО (GitHub Actions): секреты берутся из переменных
#     окружения GOOGLE_CREDENTIALS и SESSION_STRING
#   - ЛОКАЛЬНО (ноутбук): ключ Google берётся из json-файла,
#     сессия Telegram - из файла session.session
#
#  Листы таблицы:
#   Канал      - паспорт канала
#   Посты      - метрики по каждому посту (+ автозаполнение 24ч/72ч)
#   Дашборд    - KPI, топ-5, динамика по неделям
#   Динамика   - история подписчиков (append-only, никогда не стирается)
#   _Аудитория - служебный скрытый лист со слепком ID подписчиков
# ============================================================

import os
import glob
import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
import gspread
from gspread_formatting import (
    cellFormat, textFormat, Color,
    set_column_widths, set_frozen, set_row_height, set_row_heights
)
from gspread_formatting.batch_update_requests import _build_repeat_cell_request
from google.oauth2.service_account import Credentials

# ---------- Секреты и константы ----------

@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    channel: str
    spreadsheet_id: str
    service_account_file: Optional[str]
    post_fetch_limit: Optional[int]


def load_settings():
    """Загружает настройки только при реальном запуске приложения.

    Импорт модуля (например, из тестов) не читает локальный .env и не требует
    секретов. POST_FETCH_LIMIT=0 означает загрузку всей истории канала.
    """
    load_dotenv()
    required = ("API_ID", "API_HASH", "CHANNEL", "SPREADSHEET_ID")
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit(
            f"Не найдены переменные окружения: {', '.join(missing)}. "
            "Проверь локальные настройки или секреты GitHub Actions.")

    try:
        api_id = int(os.environ["API_ID"])
    except ValueError as exc:
        raise SystemExit("API_ID должен быть целым числом.") from exc

    raw_limit = os.getenv("POST_FETCH_LIMIT", "0").strip()
    try:
        parsed_limit = int(raw_limit)
    except ValueError as exc:
        raise SystemExit("POST_FETCH_LIMIT должен быть целым числом.") from exc
    post_fetch_limit = parsed_limit if parsed_limit > 0 else None

    return Settings(
        api_id=api_id,
        api_hash=os.environ["API_HASH"],
        channel=os.environ["CHANNEL"],
        spreadsheet_id=os.environ["SPREADSHEET_ID"],
        service_account_file=os.getenv("SERVICE_ACCOUNT_FILE"),
        post_fetch_limit=post_fetch_limit,
    )

# ---------- Палитра оформления ----------

NAVY       = Color(0.047, 0.224, 0.420)   # шапки таблиц
TEAL       = Color(0.000, 0.502, 0.502)   # заголовки секций
TEAL_ROW   = Color(0.878, 0.961, 0.961)   # чётные строки
BLUE_ROW   = Color(0.918, 0.945, 0.980)   # нечётные строки
CARD_BG    = Color(0.925, 0.949, 0.992)   # фон KPI-карточек
GREEN_VAL  = Color(0.047, 0.525, 0.298)   # цифры KPI
GREEN_HL   = Color(0.812, 0.941, 0.843)   # подсветка постов с высоким ERR
RED_HL     = Color(0.996, 0.882, 0.882)   # подсветка постов с низким ERR
WHITE      = Color(1, 1, 1)
GRAY       = Color(0.35, 0.35, 0.35)
DARK       = Color(0.08, 0.08, 0.08)

def mk(bg, bold=False, size=9, fg=None, h="LEFT", v="MIDDLE"):
    """Конструктор формата ячейки: фон, шрифт, выравнивание."""
    return cellFormat(
        backgroundColor=bg,
        textFormat=textFormat(bold=bold, fontSize=size,
                              foregroundColor=fg or DARK),
        horizontalAlignment=h, verticalAlignment=v,
    )

FMT_H      = mk(NAVY,      bold=True, size=10, fg=WHITE,      h="CENTER")
FMT_SEC    = mk(TEAL,      bold=True, size=10, fg=WHITE,      h="LEFT")
FMT_EVEN   = mk(TEAL_ROW,  size=9)
FMT_ODD    = mk(BLUE_ROW,  size=9)
FMT_GREEN  = mk(GREEN_HL,  size=9,   fg=Color(0.05,0.38,0.15))
FMT_RED    = mk(RED_HL,    size=9,   fg=Color(0.52,0.05,0.05))
FMT_KPI_L  = mk(CARD_BG,  size=9,   fg=GRAY, h="CENTER", v="BOTTOM")
FMT_KPI_V  = mk(CARD_BG,  bold=True, size=22, fg=GREEN_VAL, h="CENTER", v="TOP")
FMT_MANUAL = mk(Color(1.0, 0.992, 0.929), size=9)  # жёлтый: ячейки, куда можно писать руками

# ---------- Подключение к Google Sheets ----------

def find_local_key(configured_path):
    """Путь к json-ключу Google для локального запуска.

    Сначала смотрим переменную SERVICE_ACCOUNT_FILE. Если её нет - ищем
    единственный json-файл в папке со скриптом (json-ы в git не попадают,
    см. .gitignore). Несколько файлов или ни одного - понятная ошибка.
    """
    if configured_path:
        if not os.path.exists(configured_path):
            raise SystemExit(
                f"Локальный запуск: не найден файл ключа {configured_path}.")
        return configured_path

    here = os.path.dirname(os.path.abspath(__file__))
    found = sorted(glob.glob(os.path.join(here, "*.json")))
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(
            "Локальный запуск: json-ключ Google не найден в папке со скриптом. "
            "Положи его рядом или укажи путь в переменной SERVICE_ACCOUNT_FILE.")
    raise SystemExit(
        "Локальный запуск: в папке несколько json-файлов. "
        "Укажи нужный в переменной SERVICE_ACCOUNT_FILE (файл .env).")


def get_book(settings):
    """Открывает таблицу. Сам определяет режим:
    - есть переменная GOOGLE_CREDENTIALS -> облако (GitHub Actions)
    - нет -> локальный запуск, ключ читаем из json-файла рядом со скриптом

    BackOffHTTPClient - встроенный в gspread клиент с экспоненциальными
    повторами на 408, 429 и любых 5xx (включая 503 "сервис недоступен").
    Без него один временный сбой на стороне Google ронял весь прогон.
    """
    import json
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds_env = os.getenv("GOOGLE_CREDENTIALS")
    if creds_env:
        creds = Credentials.from_service_account_info(
            json.loads(creds_env), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(
            find_local_key(settings.service_account_file), scopes=scopes)
    client = gspread.authorize(creds, http_client=gspread.BackOffHTTPClient)
    return client.open_by_key(settings.spreadsheet_id)

# ---------- Мелкие помощники для Google Sheets API ----------

def get_or_create(book, title):
    """Возвращает лист по имени; если его нет - создаёт."""
    titles = [ws.title for ws in book.worksheets()]
    return book.worksheet(title) if title in titles else \
           book.add_worksheet(title=title, rows=500, cols=20)

def push(book, reqs):
    """Отправляет пачку запросов форматирования одним вызовом
    (экономит квоту Google на количество обращений)."""
    reqs = [request for request in reqs if request]
    for start in range(0, len(reqs), 400):
        book.batch_update({"requests": reqs[start:start + 400]})

def fmt(ws, a1, f):
    """Запрос: применить формат f к диапазону a1."""
    return _build_repeat_cell_request(ws, a1, f)

def merge(ws, a1):
    """Запрос: объединить ячейки диапазона."""
    return {"mergeCells": {
        "range": gspread.utils.a1_range_to_grid_range(a1, ws.id),
        "mergeType": "MERGE_ALL"}}

def unmerge(ws):
    """Запрос: снять все объединения на листе (перед перезаписью)."""
    return {"unmergeCells": {"range": {
        "sheetId": ws.id,
        "startRowIndex": 0, "endRowIndex": ws.row_count,
        "startColumnIndex": 0, "endColumnIndex": ws.col_count}}}

def hide_cols(ws, start, end):
    """Запрос: скрыть колонки с start по end (индексы с нуля)."""
    if start >= end:
        return None
    return {"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                  "startIndex": start, "endIndex": end},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}

def hide_rows(ws, start, end):
    if start >= end:
        return None
    return {"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "ROWS",
                  "startIndex": start, "endIndex": end},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}

def show_cols(ws, count):
    return {"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                  "startIndex": 0, "endIndex": count},
        "properties": {"hiddenByUser": False}, "fields": "hiddenByUser"}}

def show_rows(ws, count):
    return {"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": count},
        "properties": {"hiddenByUser": False}, "fields": "hiddenByUser"}}

def border(ws, a1):
    """Запрос: тонкие рамки вокруг и внутри диапазона."""
    b = {"style": "SOLID", "width": 1,
         "color": {"red": 0.75, "green": 0.82, "blue": 0.90}}
    return {"updateBorders": {
        "range": gspread.utils.a1_range_to_grid_range(a1, ws.id),
        "top": b, "bottom": b, "left": b, "right": b,
        "innerHorizontal": b, "innerVertical": b}}

def note_req(ws, a1, text):
    """Запрос: примечание (заметка) к ячейке."""
    r = gspread.utils.a1_range_to_grid_range(a1, ws.id)
    return {"updateCells": {
        "range": r,
        "rows": [{"values": [{"note": text}]}],
        "fields": "note"}}

def fmt_reactions(d):
    """Словарь реакций {эмодзи: число} -> строка 'эмодзи N  эмодзи N'."""
    return "—" if not d else "  ".join(f"{e} {c}" for e, c in d.items())


def as_number(value, default=0):
    """Число из Google Sheets с поддержкой запятой и пробелов в локали."""
    if isinstance(value, (int, float)):
        return value
    normalized = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not normalized:
        return default
    try:
        return float(normalized.replace(",", "."))
    except ValueError:
        return default


def engagement_rate(reactions, forwards, replies, views):
    """ERR: все измеряемые взаимодействия относительно просмотров."""
    return round((reactions + forwards + replies) / views * 100, 1) if views else 0


def week_label(date_str):
    """Дата поста -> метка недели с годом, чтобы годы не смешивались."""
    dt  = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    mon = dt - timedelta(days=dt.weekday())
    sun = mon + timedelta(days=6)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year} · Нед.{iso_week:02d} {mon.strftime('%d.%m')}–{sun.strftime('%d.%m')}"


def weekly_summary(posts):
    """Хронологическая недельная агрегация без строковой сортировки."""
    weeks = defaultdict(lambda: {
        "posts": 0, "views": 0, "reactions": 0,
        "forwards": 0, "replies": 0,
    })
    for post in posts:
        dt = datetime.strptime(post["date"], "%Y-%m-%d %H:%M")
        monday = (dt - timedelta(days=dt.weekday())).date()
        weeks[monday]["posts"] += 1
        weeks[monday]["views"] += post["views"]
        weeks[monday]["reactions"] += post["reactions_total"]
        weeks[monday]["forwards"] += post["forwards"]
        weeks[monday]["replies"] += post["replies"]

    result = []
    for monday, data in sorted(weeks.items()):
        count = data["posts"]
        sample_date = datetime.combine(monday, datetime.min.time())
        result.append([
            week_label(sample_date.strftime("%Y-%m-%d %H:%M")),
            count,
            round(data["views"] / count) if count else 0,
            engagement_rate(
                data["reactions"], data["forwards"],
                data["replies"], data["views"]),
        ])
    return result


def post_url(channel, msg_id):
    """Ссылка на пост для username, t.me URL или внутреннего -100 ID."""
    value = str(channel).strip().rstrip("/")
    if value.startswith("https://t.me/"):
        return f"{value}/{msg_id}"
    value = value.lstrip("@")
    if value.startswith("-100") and value[4:].isdigit():
        return f"https://t.me/c/{value[4:]}/{msg_id}"
    return f"https://t.me/{value}/{msg_id}"


def calculate_audience_delta(old_ids, new_ids, initialized):
    """Возвращает приток/отток; первый снимок задаёт точку отсчёта."""
    if not initialized:
        return "", ""
    return len(new_ids - old_ids), len(old_ids - new_ids)

# ============================================================
#  ЛИСТ «ПОСТЫ»
# ============================================================

POST_HEADER = [
    "ID", "Дата", "Ссылка", "Превью текста", "Просм. сейчас",
    "Просм. 24ч", "Просм. 72ч", "Реакции (всего)",
    "Реакции (детально)", "Подписчики", "1-Day Reach %",
    "Реакции / просмотры %", "Комментарий ✏️", "Пересылки",
    "Комментарии (шт)", "ERR %",
]


def _existing_posts(values):
    result = {}
    for row in values[1:]:
        if not row or not str(row[0]).strip().isdigit():
            continue
        padded = list(row[:16]) + [""] * max(0, 16 - len(row))
        for index in (0, 4, 5, 6, 7, 9, 13, 14):
            if padded[index] in (None, ""):
                continue
            number = as_number(padded[index], default=None)
            if number is not None:
                padded[index] = int(number) if float(number).is_integer() else number
        result[int(row[0])] = padded[:16]
    return result


def _analytics_post(row):
    reactions = as_number(row[7])
    forwards = as_number(row[13])
    replies = as_number(row[14])
    views = as_number(row[4])
    return {
        "id": int(row[0]),
        "date": row[1],
        "text_preview": row[3],
        "views": views,
        "forwards": forwards,
        "replies": replies,
        "reactions_total": reactions,
        "err": engagement_rate(reactions, forwards, replies, views),
    }


def build_post_rows(existing_values, posts, subscribers, channel, now=None):
    """Объединяет свежие данные с историей, не удаляя старые посты."""
    existing = _existing_posts(existing_values)
    fresh = {post["id"]: post for post in posts}
    now = now or datetime.now(timezone.utc)
    rows_by_id = {}

    for post_id in sorted(set(existing) | set(fresh), reverse=True):
        old = existing.get(post_id, [""] * 16)
        if post_id not in fresh:
            old[10] = old[11] = old[15] = ""  # формулы восстановятся ниже
            rows_by_id[post_id] = old
            continue

        post = fresh[post_id]
        post_dt = datetime.strptime(
            post["date"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        age_h = (now - post_dt).total_seconds() / 3600

        views_24h = old[5]
        if views_24h in (None, "") and 24 <= age_h < 48:
            views_24h = post["views"]

        views_72h = old[6]
        if views_72h in (None, "") and 72 <= age_h < 96:
            views_72h = post["views"]

        # Историческое число подписчиков нельзя восстановить задним числом.
        # Для впервые увиденного старого поста оставляем пусто, а не подставляем
        # сегодняшнее значение под видом значения на момент публикации.
        subs_at_post = old[9]
        if subs_at_post in (None, "") and age_h <= 12:
            subs_at_post = subscribers

        rows_by_id[post_id] = [
            post_id, post["date"], post_url(channel, post_id),
            post["text_preview"], post["views"], views_24h, views_72h,
            post["reactions_total"], post["reactions_fmt"],
            subs_at_post, "", "", old[12],
            post["forwards"], post["replies"], "",
        ]

    data_rows = [rows_by_id[post_id] for post_id in sorted(rows_by_id, reverse=True)]
    return [POST_HEADER, *data_rows], [_analytics_post(row) for row in data_rows]


def write_posts(ws, book, posts, subscribers, channel):
    existing_values = ws.get_all_values()
    rows, analytics_posts = build_post_rows(
        existing_values, posts, subscribers, channel)

    required_rows = max(len(rows) + 20, 500)
    if ws.row_count < required_rows or ws.col_count < 20:
        ws.resize(rows=max(ws.row_count, required_rows),
                  cols=max(ws.col_count, 20))

    # Один RAW update не исполняет Telegram-текст как формулу. В отличие от
    # clear()+update, ошибка запроса оставляет предыдущую таблицу целой.
    push(book, [unmerge(ws), show_cols(ws, 20), show_rows(ws, ws.row_count)])
    ws.update(values=rows, range_name="A1", value_input_option="RAW")

    formula_updates = []
    for i in range(2, len(rows) + 1):
        formula_updates.extend([
            {
                "range": f"K{i}",
                "values": [[f'=IF(F{i}="";"";IFERROR(ROUND(F{i}/J{i}*100;1);""))']],
            },
            {
                "range": f"L{i}",
                "values": [[f"=IFERROR(ROUND(H{i}/E{i}*100;1);0)"]],
            },
            {
                "range": f"P{i}",
                "values": [[f"=IFERROR(ROUND((H{i}+N{i}+O{i})/E{i}*100;1);0)"]],
            },
        ])
    for start in range(0, len(formula_updates), 500):
        ws.batch_update(
            formula_updates[start:start + 500],
            value_input_option="USER_ENTERED")

    set_column_widths(ws, [("A", 52), ("B", 130), ("C", 180), ("D", 270),
                           ("E", 100), ("F", 105), ("G", 105), ("H", 110),
                           ("I", 200), ("J", 105), ("K", 110), ("L", 130),
                           ("M", 160), ("N", 95), ("O", 120), ("P", 90)])
    set_row_height(ws, "1", 34)

    reqs = [fmt(ws, "A1:P1", FMT_H)]
    for i, post in enumerate(analytics_posts, start=2):
        err = post["err"]
        base = (FMT_GREEN if err > 20 else FMT_RED if err < 5 else
                FMT_EVEN if i % 2 == 0 else FMT_ODD)
        reqs.extend([
            fmt(ws, f"A{i}:P{i}", base),
            fmt(ws, f"F{i}", FMT_MANUAL),
            fmt(ws, f"G{i}", FMT_MANUAL),
            fmt(ws, f"M{i}", FMT_MANUAL),
        ])

    last = len(rows)
    reqs.extend([
        border(ws, f"A1:P{last}"),
        hide_cols(ws, 16, ws.col_count),
        note_req(ws, "F1", "Фиксируется автоматически через ~24 часа; ручное значение сохраняется."),
        note_req(ws, "G1", "Фиксируется автоматически через ~72 часа; ручное значение сохраняется."),
        note_req(ws, "M1", "Ручные заметки и наблюдения по посту"),
    ])
    if last < ws.row_count:
        reqs.append(hide_rows(ws, last, ws.row_count))
    push(book, reqs)
    set_frozen(ws, rows=1)
    return analytics_posts

# ============================================================
#  ЛИСТ «ДАШБОРД»
# ============================================================

def write_dashboard(ws, book, posts, subs):
    push(book,[unmerge(ws), show_cols(ws, ws.col_count),
               show_rows(ws, ws.row_count)])

    # KPI-карточки: подписчики, постов всего, средние просмотры, взвешенный ERR
    n      = len(posts)
    avg_v  = round(sum(p["views"] for p in posts)/n) if n else 0
    total_views = sum(p["views"] for p in posts)
    avg_err = engagement_rate(
        sum(p["reactions_total"] for p in posts),
        sum(p["forwards"] for p in posts),
        sum(p["replies"] for p in posts),
        total_views,
    )

    kpi = [
        ("👥 Подписчики",        subs,         "A","B",0,1),
        ("📝 Постов",            n,             "C","D",2,3),
        ("👁 Средние просмотры", avg_v,         "E","F",4,5),
        ("⚡ Взвешенный ERR%",   f"{avg_err}%", "G","H",6,7),
    ]
    for lbl,val,c1,c2,_,__ in kpi:
        ws.update(values=[[lbl]], range_name=f"{c1}1")
        ws.update(values=[[val]], range_name=f"{c1}2")
    time.sleep(1)

    reqs = []
    for lbl,val,c1,c2,ci1,ci2 in kpi:
        reqs += [merge(ws,f"{c1}1:{c2}1"), merge(ws,f"{c1}2:{c2}2"),
                 fmt(ws,f"{c1}1:{c2}1",FMT_KPI_L),
                 fmt(ws,f"{c1}2:{c2}2",FMT_KPI_V)]
    push(book,reqs); time.sleep(1)

    set_column_widths(ws,[("A",148),("B",148),("C",148),("D",148),
                          ("E",148),("F",148),("G",148),("H",148)])
    set_row_heights(ws,[("1",26),("2",56),("3",10)])

    # Топ-5 постов по просмотрам
    top5 = sorted(posts,key=lambda x: x["views"],reverse=True)[:5]
    ws.update(values=[["🏆 ТОП-5 постов по просмотрам"]], range_name="A4")
    ws.update(values=[["Дата","Просмотры","ERR%","Превью текста"]], range_name="A5")
    top_rows = [[t["date"], t["views"], t["err"], t["text_preview"]]
                for t in top5]
    top_rows.extend([["", "", "", ""]] * (5 - len(top_rows)))
    ws.update(values=top_rows, range_name="A6")
    time.sleep(1)

    reqs = [merge(ws,"A4:H4"), fmt(ws,"A4:H4",FMT_SEC),
            merge(ws,"D5:H5"), fmt(ws,"A5:H5",FMT_H)]
    for i in range(len(top5)):
        row=6+i
        reqs += [merge(ws,f"D{row}:H{row}"),
                 fmt(ws,f"A{row}:H{row}",FMT_EVEN if i%2==0 else FMT_ODD)]
    reqs.append(border(ws,"A4:H10"))
    push(book,reqs); time.sleep(1)
    set_row_height(ws,"4",30)
    set_column_widths(ws,[("D",360)])

    # Понедельная сводка: сортировка по дате, ERR взвешен по просмотрам.
    week_data = weekly_summary(posts)

    SR=12
    ws.update(values=[["📅 Динамика по неделям"]], range_name=f"A{SR}")
    ws.update(values=[["Неделя","Постов","Средние просмотры","Взвешенный ERR%"]],
              range_name=f"A{SR+1}")
    if week_data:
        ws.update(values=week_data, range_name=f"A{SR+2}")
    time.sleep(1)

    last_w=SR+1+len(week_data)
    reqs=[merge(ws,f"A{SR}:H{SR}"), fmt(ws,f"A{SR}:H{SR}",FMT_SEC),
          fmt(ws,f"A{SR+1}:D{SR+1}",FMT_H)]
    for i in range(len(week_data)):
        row=SR+2+i
        reqs.append(fmt(ws,f"A{row}:D{row}",FMT_EVEN if i%2==0 else FMT_ODD))
    reqs += [border(ws, f"A{SR}:D{last_w}"),
             hide_cols(ws, 8, ws.col_count)]
    if last_w < ws.row_count:
        reqs.append(hide_rows(ws, last_w, ws.row_count))
    push(book,reqs)
    set_row_height(ws,str(SR),30)
    set_frozen(ws,rows=3)

# ============================================================
#  ЛИСТ «ДИНАМИКА» + служебный слепок аудитории
# ============================================================

def write_dynamics(book, current_ids, subs):
    """История подписчиков.

    Как работает:
     1. Читает прошлый слепок ID подписчиков со скрытого листа _Аудитория.
     2. Сравнивает с текущим: кто появился -> 'Пришло', кто исчез -> 'Ушло'.
     3. ДОПИСЫВАЕТ строку в лист 'Динамика' (append-only: скрипт никогда
        не стирает историю - это защита данных на случай любых сбоев).
     4. Сохраняет новый слепок и прячет служебный лист.

    Анонимность: хранятся только числовые Telegram ID, без имён и username.
    """
    # 1. Прошлый слепок
    aud = get_or_create(book, "_Аудитория")
    old_vals = aud.col_values(1)
    old_ids = set(int(v) for v in old_vals if str(v).strip().isdigit())
    new_ids = set(current_ids)
    initialized = aud.acell("B1").value == "initialized" or bool(old_ids)

    # 2. Лист "Динамика": проверяем шапку по содержимому ячейки A1.
    #    Если её нет (первый запуск или шапка потерялась) - ВСТАВЛЯЕМ строку
    #    сверху, не трогая уже накопленные данные, и оформляем лист.
    dyn = get_or_create(book, "Динамика")
    if dyn.acell("A1").value != "Дата (UTC)":
        dyn.insert_row(["Дата (UTC)", "Подписчики", "Пришло", "Ушло"], index=1)
        time.sleep(1)
        set_column_widths(dyn, [("A", 150), ("B", 110), ("C", 90), ("D", 90)])
        set_row_height(dyn, "1", 34)
        reqs = [fmt(dyn, "A1:D1", FMT_H), hide_cols(dyn, 4, dyn.col_count)]
        # Автоматическая "зебра" на будущие строки: полосатый диапазон
        # сам красит каждую новую строку, ничего дописывать не нужно
        try:
            reqs.append({"addBanding": {"bandedRange": {
                "range": {"sheetId": dyn.id,
                          "startRowIndex": 1, "endRowIndex": dyn.row_count,
                          "startColumnIndex": 0, "endColumnIndex": 4},
                "rowProperties": {
                    "firstBandColor":  {"red": 0.878, "green": 0.961, "blue": 0.961},
                    "secondBandColor": {"red": 0.918, "green": 0.945, "blue": 0.980},
                }}}})
            push(book, reqs)
        except Exception:
            # Если зебра уже существует, Google вернёт ошибку -
            # тогда применяем только шапку и скрытие колонок
            push(book, reqs[:-1])
        set_frozen(dyn, rows=1)

    # 3. Приток/отток. Первый снимок задаёт точку отсчёта.
    joined, left = calculate_audience_delta(old_ids, new_ids, initialized)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    if aud.row_count < max(len(new_ids), 2):
        aud.resize(rows=max(len(new_ids) + 100, 2),
                   cols=max(aud.col_count, 2))

    def entered(value):
        if isinstance(value, (int, float)):
            return {"numberValue": value}
        return {"stringValue": str(value)}

    # Очистка старого снимка, запись нового и append истории входят в один
    # spreadsheets.batchUpdate. Google применяет такой пакет целиком: не будет
    # состояния, где история уже дописана, а снимок ещё старый (или наоборот).
    requests = [{
        "repeatCell": {
            "range": {
                "sheetId": aud.id,
                "startRowIndex": 0,
                "endRowIndex": aud.row_count,
                "startColumnIndex": 0,
                "endColumnIndex": 1,
            },
            "cell": {},
            "fields": "userEnteredValue",
        }
    }]
    if new_ids:
        requests.append({
            "updateCells": {
                "start": {"sheetId": aud.id, "rowIndex": 0, "columnIndex": 0},
                # ID храним строкой: Google Sheets использует double для чисел,
                # а строка гарантирует точность 64-битного Telegram ID.
                "rows": [{"values": [{"userEnteredValue": entered(str(user_id))}]}
                         for user_id in sorted(new_ids)],
                "fields": "userEnteredValue",
            }
        })
    requests.extend([
        {
            "updateCells": {
                "start": {"sheetId": aud.id, "rowIndex": 0, "columnIndex": 1},
                "rows": [{"values": [{"userEnteredValue": entered("initialized")}]},
                         {"values": [{"userEnteredValue": entered(captured_at)}]}],
                "fields": "userEnteredValue",
            }
        },
        {
            "appendCells": {
                "sheetId": dyn.id,
                "rows": [{"values": [
                    {"userEnteredValue": entered(captured_at)},
                    {"userEnteredValue": entered(subs)},
                    {"userEnteredValue": entered(joined)},
                    {"userEnteredValue": entered(left)},
                ]}],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": aud.id, "hidden": True},
                "fields": "hidden",
            }
        },
    ])
    book.batch_update({"requests": requests})

# ============================================================
#  ЛИСТ «КАНАЛ»
# ============================================================

def write_channel(ws, book, ch, subs, desc):
    push(book,[unmerge(ws), show_cols(ws, ws.col_count),
               show_rows(ws, ws.row_count)])
    ws.update(values=[
        ["Параметр",   "Значение",          "Обновлено"],
        ["Название",   ch.title,            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")],
        ["Username",   f"@{ch.username}",   ""],
        ["Подписчики", subs,                ""],
        ["Описание",   desc,                ""],
    ], range_name="A1"); time.sleep(1)
    set_column_widths(ws,[("A",180),("B",380),("C",170)])
    set_row_height(ws,"1",34)
    push(book,[
        fmt(ws,"A1:C1",FMT_H),
        fmt(ws,"A2:C2",FMT_EVEN), fmt(ws,"A3:C3",FMT_ODD),
        fmt(ws,"A4:C4",FMT_EVEN), fmt(ws,"A5:C5",FMT_ODD),
        border(ws,"A1:C5"),
        hide_cols(ws, 3, ws.col_count), hide_rows(ws, 5, ws.row_count),
    ])
    set_frozen(ws,rows=1)

# ============================================================
#  ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def make_client(settings):
    """Выбор Telegram-сессии по режиму:
    - облако: строка SESSION_STRING из секретов (без интерактивного входа)
    - локально: файловая сессия session.session - телефон и код
      спросит только ОДИН раз, дальше входит сам.
    """
    session_str = os.getenv("SESSION_STRING")
    if session_str:
        return TelegramClient(
            StringSession(session_str), settings.api_id, settings.api_hash)
    return TelegramClient("session", settings.api_id, settings.api_hash)


async def collect_subscriber_ids(tg, channel, expected_count):
    """Возвращает полный проверенный снимок или None при любом сомнении."""
    collected = []
    try:
        async for user in tg.iter_participants(channel):
            collected.append(user.id)
    except Exception as exc:
        # Частичный результат намеренно отбрасывается: иначе он выглядит как
        # массовый отток и повреждает следующий снимок.
        print(f"⚠️ Снимок аудитории пропущен: {exc}")
        return None

    result = set(collected)
    if len(result) != len(collected):
        print("⚠️ Снимок аудитории пропущен: Telegram вернул дубли ID")
        return None

    if expected_count is not None:
        tolerance = max(1, round(expected_count * 0.005))
        if abs(len(result) - expected_count) > tolerance:
            print(
                "⚠️ Снимок аудитории пропущен: "
                f"ожидалось около {expected_count}, получено {len(result)}")
            return None

    return result


async def main(settings=None):
    settings = settings or load_settings()
    async with make_client(settings) as tg:

        # --- Паспорт канала ---
        full=await tg(GetFullChannelRequest(settings.channel))
        ch=full.chats[0]; subs=full.full_chat.participants_count
        desc=full.full_chat.about or ""
        print(f"Канал: {ch.title} | Подписчики: {subs}")

        # --- Сбор постов ---
        posts=[]
        async for msg in tg.iter_messages(
                settings.channel, limit=settings.post_fetch_limit):
            # Медиа-пост без подписи тоже является постом; пропускаются только
            # служебные события без текста и медиа.
            if not msg.message and not msg.media:
                continue
            views=msg.views or 0; forwards=msg.forwards or 0
            replies=(msg.replies.replies if msg.replies else 0)
            rt,rd=0,{}
            if msg.reactions:
                for r in msg.reactions.results:
                    e = (r.reaction.emoticon if hasattr(r.reaction, "emoticon")
                         else type(r.reaction).__name__)
                    rd[e] = rd.get(e, 0) + r.count
                    rt += r.count
            posts.append({
                "id":msg.id,
                # Дата всегда в UTC - от этого зависит расчёт окон 24ч/72ч
                "date":msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "text_preview":((msg.message or "[медиа без подписи]")[:80]
                                .replace("\n", " ")),
                "views":views,"forwards":forwards,"replies":replies,
                "reactions_total":rt,"reactions_fmt":fmt_reactions(rd),
                "err":engagement_rate(rt, forwards, replies, views),
            })
        print(f"Постов: {len(posts)}")

        # --- Слепок аудитории (только псевдонимные ID) ---
        # Доступно владельцу/админу канала. Обёрнуто в try/except:
        # если Telegram не отдаст список - скрипт продолжит работу без Динамики.
        subscriber_ids = await collect_subscriber_ids(tg, ch, subs)
        if subscriber_ids is not None:
            print(f"Слепок аудитории: {len(subscriber_ids)} ID")

        # --- Запись в Google Sheets ---
        # time.sleep(3) между листами - защита от лимита Google
        # на количество запросов в минуту.
        book=get_book(settings)

        write_channel(get_or_create(book,"Канал"),book,ch,subs,desc)
        print("✅ Канал"); time.sleep(3)

        all_posts = write_posts(
            get_or_create(book, "Посты"), book, posts, subs, settings.channel)
        print("✅ Посты"); time.sleep(3)

        write_dashboard(get_or_create(book,"Дашборд"),book,all_posts,subs)
        print("✅ Дашборд"); time.sleep(3)

        if subscriber_ids is not None:
            write_dynamics(book, subscriber_ids, subs)
            print("✅ Динамика")

        print("\n📊 Данные успешно обновлены")

if __name__=="__main__":
    asyncio.run(main())
