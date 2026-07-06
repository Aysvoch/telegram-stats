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
import asyncio
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
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

# Подхватываем .env (локальный запуск). В облаке файла нет - не страшно.
load_dotenv()

# ---------- Секреты и константы ----------

# Понятная ошибка вместо криптоватого TypeError, если .env не подхватился
_missing = [k for k in ("API_ID", "API_HASH", "CHANNEL") if not os.getenv(k)]
if _missing:
    raise SystemExit(
        f"Не найдены переменные окружения: {', '.join(_missing)}. "
        "Проверь файл .env (локально) или секреты репозитория (GitHub Actions).")

API_ID   = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
CHANNEL  = os.getenv("CHANNEL")

SERVICE_ACCOUNT_FILE = "telegramstats-500216-4fac021bdfc6.json"
SPREADSHEET_ID       = "1qiJmZREHIxfEsr90zr-O34FEF-6YG3nwRuGSSaAyY3s"

# ---------- Палитра оформления ----------

NAVY       = Color(0.047, 0.224, 0.420)   # шапки таблиц
TEAL       = Color(0.000, 0.502, 0.502)   # заголовки секций
TEAL_ROW   = Color(0.878, 0.961, 0.961)   # чётные строки
BLUE_ROW   = Color(0.918, 0.945, 0.980)   # нечётные строки
CARD_BG    = Color(0.925, 0.949, 0.992)   # фон KPI-карточек
GREEN_VAL  = Color(0.047, 0.525, 0.298)   # цифры KPI
GREEN_HL   = Color(0.812, 0.941, 0.843)   # подсветка постов с высоким ER
RED_HL     = Color(0.996, 0.882, 0.882)   # подсветка постов с низким ER
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

def get_book():
    """Открывает таблицу. Сам определяет режим:
    - есть переменная GOOGLE_CREDENTIALS -> облако (GitHub Actions)
    - нет -> локальный запуск, ключ читаем из json-файла рядом со скриптом
    """
    import json
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds_env = os.getenv("GOOGLE_CREDENTIALS")
    if creds_env:
        creds = Credentials.from_service_account_info(
            json.loads(creds_env), scopes=scopes)
    else:
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
            raise SystemExit(
                f"Локальный запуск: не найден файл ключа {SERVICE_ACCOUNT_FILE}. "
                "Скопируй его в папку со скриптом.")
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

# ---------- Мелкие помощники для Google Sheets API ----------

def get_or_create(book, title):
    """Возвращает лист по имени; если его нет - создаёт."""
    titles = [ws.title for ws in book.worksheets()]
    return book.worksheet(title) if title in titles else \
           book.add_worksheet(title=title, rows=500, cols=20)

def push(book, reqs):
    """Отправляет пачку запросов форматирования одним вызовом
    (экономит квоту Google на количество обращений)."""
    if reqs:
        book.batch_update({"requests": reqs})

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
        "startRowIndex": 0, "endRowIndex": 500,
        "startColumnIndex": 0, "endColumnIndex": 20}}}

def hide_cols(ws, start, end):
    """Запрос: скрыть колонки с start по end (индексы с нуля)."""
    return {"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                  "startIndex": start, "endIndex": end},
        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}

def hide_rows(ws, start, end):
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

def week_label(date_str):
    """Дата поста -> метка недели вида 'Нед.27 29.06–05.07'."""
    dt  = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    mon = dt - timedelta(days=dt.weekday())
    sun = mon + timedelta(days=6)
    return f"Нед.{dt.isocalendar()[1]} {mon.strftime('%d.%m')}–{sun.strftime('%d.%m')}"

def post_url(msg_id):
    return f"https://t.me/{CHANNEL}/{msg_id}"

# ============================================================
#  ЛИСТ «ПОСТЫ»
# ============================================================

def write_posts(ws, book, posts, subscribers):
    # --- Шаг 1. Сохраняем то, что нельзя потерять при перезаписи ---
    # Лист полностью очищается на каждом запуске, поэтому сначала читаем
    # текущие значения ячеек, которые заполняются один раз или вручную:
    # F (просм. 24ч), G (просм. 72ч), J (подписчики на момент поста), M (заметки).
    existing = ws.get_all_values()
    existing_map = {}
    for i, row in enumerate(existing[1:], start=2):
        if row and row[0]:
            try:
                existing_map[int(row[0])] = {
                    "row":       i,
                    "views_24h": row[5] if len(row) > 5 else "",
                    "views_72h": row[6] if len(row) > 6 else "",
                    "subs":      row[9] if len(row) > 9 else "",
                    "comment":   row[12] if len(row) > 12 else "",
                }
            except (ValueError, IndexError):
                pass

    push(book, [unmerge(ws), show_cols(ws, 20), show_rows(ws, 500)])
    ws.clear(); time.sleep(1)

    # Новые колонки (N, O, P) добавлены строго В КОНЕЦ таблицы, чтобы
    # existing_map выше продолжал читать старые данные по прежним индексам A-M.
    header = ["ID","Дата","Ссылка","Превью текста","Просм. сейчас",
              "Просм. 24ч","Просм. 72ч","Реакции (всего)",
              "Реакции (детально)","Подписчики","1-Day Reach %","ER поста %","Комментарий ✏️",
              "Пересылки","Комментарии (шт)","ERR %"]

    # --- Шаг 2. Собираем строки + автозаполнение 24ч/72ч ---
    rows = [header]
    now = datetime.now(timezone.utc)
    for p in posts:
        ex = existing_map.get(p["id"], {})
        subs_val = ex.get("subs") or subscribers

        # Возраст поста в часах. Дата в p["date"] всегда в UTC.
        post_dt = datetime.strptime(p["date"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        age_h = (now - post_dt).total_seconds() / 3600

        # Логика автозаполнения:
        #  - пишем ТЕКУЩИЕ просмотры, только если ячейка ещё пустая
        #    и пост находится в окне 24-48ч (для F) / 72-96ч (для G);
        #  - широкое окно = 3-4 попытки при запуске каждые 6 часов,
        #    один сбойный запуск ничего не ломает;
        #  - заполненные (в т.ч. вручную) значения НИКОГДА не перезаписываем;
        #  - окно упущено -> ячейка честно остаётся пустой.
        views_24h = ex.get("views_24h", "")
        if not views_24h and 24 <= age_h < 48:
            views_24h = p["views"]

        views_72h = ex.get("views_72h", "")
        if not views_72h and 72 <= age_h < 96:
            views_72h = p["views"]

        rows.append([
            p["id"], p["date"], post_url(p["id"]), p["text_preview"],
            p["views"],
            views_24h,
            views_72h,
            p["reactions_total"], p["reactions_fmt"],
            subs_val, "", "", ex.get("comment", ""),
            p["forwards"], p["replies"], "",
        ])

    ws.update(values=rows, range_name="A1"); time.sleep(1)

    # --- Шаг 3. Формулы ---
    # ВАЖНО: разделитель аргументов - точка с запятой ";", а не запятая.
    # Таблица в русской локали, где запятая = десятичный знак,
    # и формулы с запятыми дают "Синтаксическую ошибку".
    # Точку с запятой Google Sheets принимает в любой локали.
    #
    # K (1-Day Reach %): пока F пустая - показываем пустоту, а не ноль,
    #   чтобы не портить средние значения фальшивыми нулями.
    # L (ER %): реакции / подписчики - старая метрика, оставлена для
    #   сопоставимости с накопленной историей.
    # P (ERR %): (реакции + пересылки + комментарии) / просмотры -
    #   более полная метрика вовлечённости; IFERROR защищает от деления на 0.
    formula_updates = []
    for i in range(2, len(posts) + 2):
        formula_updates.append({
            "range": f"K{i}",
            "values": [[f'=IF(F{i}="";"";ROUND(F{i}/J{i}*100;1))']]
        })
        formula_updates.append({
            "range": f"L{i}",
            "values": [[f"=IFERROR(ROUND(H{i}/J{i}*100;1);0)"]]
        })
        formula_updates.append({
            "range": f"P{i}",
            "values": [[f"=IFERROR(ROUND((H{i}+N{i}+O{i})/E{i}*100;1);0)"]]
        })
    if formula_updates:
        ws.batch_update(formula_updates, value_input_option="USER_ENTERED")
    time.sleep(1)

    # --- Шаг 4. Оформление ---
    set_column_widths(ws,[("A",52),("B",130),("C",180),("D",270),
                          ("E",100),("F",105),("G",105),("H",110),
                          ("I",200),("J",105),("K",110),("L",100),("M",160),
                          ("N",95),("O",120),("P",90)])
    set_row_height(ws,"1",34)

    reqs = [fmt(ws,"A1:P1", FMT_H)]
    for i, p in enumerate(posts, start=2):
        er = p["er"]
        # Подсветка строки по вовлечённости: зелёная/красная/обычная зебра
        base = (FMT_GREEN if er > 50 else FMT_RED if er < 20 else
                FMT_EVEN if i%2==0 else FMT_ODD)
        reqs.append(fmt(ws, f"A{i}:P{i}", base))
        # Жёлтым - ячейки, куда допустим ручной ввод
        reqs.append(fmt(ws, f"F{i}", FMT_MANUAL))
        reqs.append(fmt(ws, f"G{i}", FMT_MANUAL))
        reqs.append(fmt(ws, f"M{i}", FMT_MANUAL))

    last = len(posts) + 1
    reqs += [
        border(ws, f"A1:P{last}"),
        hide_cols(ws, 16, 20),     # скрываем всё правее колонки P
        hide_rows(ws, last, 500),  # скрываем пустые строки снизу
        note_req(ws,"F1","Заполняется автоматически через ~24 часа после публикации. Можно ввести вручную - скрипт не перезапишет."),
        note_req(ws,"G1","Заполняется автоматически через ~72 часа после публикации. Можно ввести вручную - скрипт не перезапишет."),
        note_req(ws,"M1","Вводи вручную: заметки и наблюдения по посту"),
    ]
    push(book, reqs)
    set_frozen(ws, rows=1)

# ============================================================
#  ЛИСТ «ДАШБОРД»
# ============================================================

def write_dashboard(ws, book, posts, subs):
    push(book,[unmerge(ws), show_cols(ws,20), show_rows(ws,500)])
    ws.clear(); time.sleep(1)

    # KPI-карточки: подписчики, постов всего, средние просмотры, средний ER
    n      = len(posts)
    avg_v  = round(sum(p["views"] for p in posts)/n) if n else 0
    avg_er = round(sum(p["er"]   for p in posts)/n,1) if n else 0

    kpi = [
        ("👥 Подписчики",        subs,         "A","B",0,1),
        ("📝 Постов",            n,             "C","D",2,3),
        ("👁 Средние просмотры", avg_v,         "E","F",4,5),
        ("⚡ Средний ER%",       f"{avg_er}%",  "G","H",6,7),
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
    ws.update(values=[["Дата","Просмотры","ER%","Превью текста"]], range_name="A5")
    for i,t in enumerate(top5):
        ws.update(values=[[t["date"],t["views"],t["er"],t["text_preview"]]],
                  range_name=f"A{6+i}")
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

    # Понедельная сводка: постов, средние просмотры, средний ER
    weeks = defaultdict(lambda:{"posts":0,"views":0,"er":0.0})
    for p in posts:
        lb=week_label(p["date"])
        weeks[lb]["posts"]+=1; weeks[lb]["views"]+=p["views"]; weeks[lb]["er"]+=p["er"]

    week_data=[[lb,d["posts"],
                round(d["views"]/d["posts"]) if d["posts"] else 0,
                round(d["er"]/d["posts"],1)  if d["posts"] else 0]
               for lb,d in sorted(weeks.items())]

    SR=12
    ws.update(values=[["📅 Динамика по неделям"]], range_name=f"A{SR}")
    ws.update(values=[["Неделя","Постов","Средние просмотры","Средний ER%"]],
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
    reqs+=[border(ws,f"A{SR}:D{last_w}"),
           hide_cols(ws,8,20), hide_rows(ws,last_w,500)]
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

    # 2. Лист "Динамика": проверяем шапку по содержимому ячейки A1.
    #    Если её нет (первый запуск или шапка потерялась) - ВСТАВЛЯЕМ строку
    #    сверху, не трогая уже накопленные данные, и оформляем лист.
    dyn = get_or_create(book, "Динамика")
    if dyn.acell("A1").value != "Дата (UTC)":
        dyn.insert_row(["Дата (UTC)", "Подписчики", "Пришло", "Ушло"], index=1)
        time.sleep(1)
        set_column_widths(dyn, [("A", 150), ("B", 110), ("C", 90), ("D", 90)])
        set_row_height(dyn, "1", 34)
        reqs = [fmt(dyn, "A1:D1", FMT_H), hide_cols(dyn, 4, 20)]
        # Автоматическая "зебра" на будущие строки: полосатый диапазон
        # сам красит каждую новую строку, ничего дописывать не нужно
        try:
            reqs.append({"addBanding": {"bandedRange": {
                "range": {"sheetId": dyn.id,
                          "startRowIndex": 1, "endRowIndex": 500,
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

    # 3. Приток/отток. Самый первый запуск - точка отсчёта:
    #    сравнивать не с чем, оставляем Пришло/Ушло пустыми.
    if old_ids:
        joined = len(new_ids - old_ids)
        left   = len(old_ids - new_ids)
    else:
        joined, left = "", ""

    dyn.append_row(
        [datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), subs, joined, left],
        value_input_option="USER_ENTERED")

    # 4. Обновляем слепок и держим лист скрытым
    aud.clear(); time.sleep(1)
    if new_ids:
        aud.update(values=[[i] for i in sorted(new_ids)], range_name="A1")
    push(book, [{"updateSheetProperties": {
        "properties": {"sheetId": aud.id, "hidden": True},
        "fields": "hidden"}}])

# ============================================================
#  ЛИСТ «КАНАЛ»
# ============================================================

def write_channel(ws, book, ch, subs, desc):
    push(book,[unmerge(ws), show_cols(ws,20), show_rows(ws,500)])
    ws.clear(); time.sleep(1)
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
        hide_cols(ws,3,20), hide_rows(ws,5,500),
    ])
    set_frozen(ws,rows=1)

# ============================================================
#  ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def make_client():
    """Выбор Telegram-сессии по режиму:
    - облако: строка SESSION_STRING из секретов (без интерактивного входа)
    - локально: файловая сессия session.session - телефон и код
      спросит только ОДИН раз, дальше входит сам.
    """
    session_str = os.getenv("SESSION_STRING")
    if session_str:
        return TelegramClient(StringSession(session_str), API_ID, API_HASH)
    return TelegramClient("session", API_ID, API_HASH)

async def main():
    async with make_client() as tg:

        # --- Паспорт канала ---
        full=await tg(GetFullChannelRequest(CHANNEL))
        ch=full.chats[0]; subs=full.full_chat.participants_count
        desc=full.full_chat.about or ""
        print(f"Канал: {ch.title} | Подписчики: {subs}")

        # --- Сбор постов ---
        posts=[]
        async for msg in tg.iter_messages(CHANNEL,limit=200):
            if not msg.message: continue   # пропускаем служебные сообщения без текста
            views=msg.views or 0; forwards=msg.forwards or 0
            replies=(msg.replies.replies if msg.replies else 0)
            rt,rd=0,{}
            if msg.reactions:
                for r in msg.reactions.results:
                    e=r.reaction.emoticon if hasattr(r.reaction,"emoticon") else "?"
                    rd[e]=r.count; rt+=r.count
            posts.append({
                "id":msg.id,
                # Дата всегда в UTC - от этого зависит расчёт окон 24ч/72ч
                "date":msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "text_preview":(msg.message or "")[:80].replace("\n"," "),
                "views":views,"forwards":forwards,"replies":replies,
                "reactions_total":rt,"reactions_fmt":fmt_reactions(rd),
                "er":round(rt/views*100,1) if views else 0,
            })
        print(f"Постов: {len(posts)}")

        # --- Слепок аудитории (только ID, анонимно) ---
        # Доступно владельцу/админу канала. Обёрнуто в try/except:
        # если Telegram не отдаст список - скрипт продолжит работу без Динамики.
        subscriber_ids = []
        try:
            async for u in tg.iter_participants(ch):
                subscriber_ids.append(u.id)
            print(f"Слепок аудитории: {len(subscriber_ids)} ID")
        except Exception as e:
            print(f"⚠️ Не удалось получить список подписчиков: {e}")

        # --- Запись в Google Sheets ---
        # time.sleep(3) между листами - защита от лимита Google
        # на количество запросов в минуту.
        book=get_book()

        write_channel(get_or_create(book,"Канал"),book,ch,subs,desc)
        print("✅ Канал"); time.sleep(3)

        write_posts(get_or_create(book,"Посты"),book,posts,subs)
        print("✅ Посты"); time.sleep(3)

        write_dashboard(get_or_create(book,"Дашборд"),book,posts,subs)
        print("✅ Дашборд"); time.sleep(3)

        if subscriber_ids:
            write_dynamics(book, subscriber_ids, subs)
            print("✅ Динамика")

        print(f"\n📊 https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")

if __name__=="__main__":
    asyncio.run(main())