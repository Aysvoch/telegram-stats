import os
import asyncio
import time
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest
import gspread
from gspread_formatting import (
    cellFormat, textFormat, Color,
    set_column_widths, set_frozen, set_row_height, set_row_heights
)
from gspread_formatting.batch_update_requests import _build_repeat_cell_request
from google.oauth2.service_account import Credentials

load_dotenv()

API_ID    = int(os.getenv("API_ID"))
API_HASH  = os.getenv("API_HASH")
CHANNEL   = os.getenv("CHANNEL")
PHONE     = os.getenv("PHONE")

SERVICE_ACCOUNT_FILE = "telegramstats-500216-4fac021bdfc6.json"
SPREADSHEET_ID       = "1qiJmZREHIxfEsr90zr-O34FEF-6YG3nwRuGSSaAyY3s"

NAVY       = Color(0.047, 0.224, 0.420)
TEAL       = Color(0.000, 0.502, 0.502)
TEAL_ROW   = Color(0.878, 0.961, 0.961)
BLUE_ROW   = Color(0.918, 0.945, 0.980)
CARD_BG    = Color(0.925, 0.949, 0.992)
GREEN_VAL  = Color(0.047, 0.525, 0.298)
GREEN_HL   = Color(0.812, 0.941, 0.843)
RED_HL     = Color(0.996, 0.882, 0.882)
WHITE      = Color(1, 1, 1)
GRAY       = Color(0.35, 0.35, 0.35)
DARK       = Color(0.08, 0.08, 0.08)

def mk(bg, bold=False, size=9, fg=None, h="LEFT", v="MIDDLE"):
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
FMT_MANUAL = mk(Color(1.0, 0.992, 0.929), size=9)

def get_book():
    import json
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
               "https://www.googleapis.com/auth/drive"]
    creds_json = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SPREADSHEET_ID)

def get_or_create(book, title):
    titles = [ws.title for ws in book.worksheets()]
    return book.worksheet(title) if title in titles else \
           book.add_worksheet(title=title, rows=500, cols=20)

def push(book, reqs):
    if reqs:
        book.batch_update({"requests": reqs})

def fmt(ws, a1, f):
    return _build_repeat_cell_request(ws, a1, f)

def merge(ws, a1):
    return {"mergeCells": {
        "range": gspread.utils.a1_range_to_grid_range(a1, ws.id),
        "mergeType": "MERGE_ALL"}}

def unmerge(ws):
    return {"unmergeCells": {"range": {
        "sheetId": ws.id,
        "startRowIndex": 0, "endRowIndex": 500,
        "startColumnIndex": 0, "endColumnIndex": 20}}}

def hide_cols(ws, start, end):
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
    b = {"style": "SOLID", "width": 1,
         "color": {"red": 0.75, "green": 0.82, "blue": 0.90}}
    return {"updateBorders": {
        "range": gspread.utils.a1_range_to_grid_range(a1, ws.id),
        "top": b, "bottom": b, "left": b, "right": b,
        "innerHorizontal": b, "innerVertical": b}}

def note_req(ws, a1, text):
    r = gspread.utils.a1_range_to_grid_range(a1, ws.id)
    return {"updateCells": {
        "range": r,
        "rows": [{"values": [{"note": text}]}],
        "fields": "note"}}

def fmt_reactions(d):
    return "—" if not d else "  ".join(f"{e} {c}" for e, c in d.items())

def week_label(date_str):
    dt  = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    mon = dt - timedelta(days=dt.weekday())
    sun = mon + timedelta(days=6)
    return f"Нед.{dt.isocalendar()[1]} {mon.strftime('%d.%m')}–{sun.strftime('%d.%m')}"

def post_url(msg_id):
    return f"https://t.me/{CHANNEL}/{msg_id}"

def write_posts(ws, book, posts, subscribers):
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

    push(book, [unmerge(ws), show_cols(ws,20), show_rows(ws,500)])
    ws.clear(); time.sleep(1)

    header = ["ID","Дата","Ссылка","Превью текста","Просм. сейчас",
              "Просм. 24ч ✏️","Просм. 72ч ✏️","Реакции (всего)",
              "Реакции (детально)","Подписчики","1-Day Reach %","ER поста %","Комментарий ✏️"]

    rows = [header]
    for p in posts:
        ex = existing_map.get(p["id"], {})
        subs_val = ex.get("subs") or subscribers
        rows.append([
            p["id"], p["date"], post_url(p["id"]), p["text_preview"],
            p["views"],
            ex.get("views_24h", ""),
            ex.get("views_72h", ""),
            p["reactions_total"], p["reactions_fmt"],
            subs_val, "", "", ex.get("comment", ""),
        ])

    ws.update(values=rows, range_name="A1"); time.sleep(1)

    formula_updates = []
    for i in range(2, len(posts) + 2):
        formula_updates.append({
            "range": f"K{i}",
            "values": [[f"=ROUND(F{i}/J{i}*100,1)"]]
        })
        formula_updates.append({
            "range": f"L{i}",
            "values": [[f"=ROUND(H{i}/J{i}*100,1)"]]
        })
    if formula_updates:
        ws.batch_update(formula_updates, value_input_option="USER_ENTERED")
    time.sleep(1)

    set_column_widths(ws,[("A",52),("B",130),("C",180),("D",270),
                          ("E",100),("F",105),("G",105),("H",110),
                          ("I",200),("J",105),("K",110),("L",100),("M",160)])
    set_row_height(ws,"1",34)

    reqs = [fmt(ws,"A1:M1", FMT_H)]
    for i, p in enumerate(posts, start=2):
        er = p["er"]
        base = (FMT_GREEN if er > 50 else FMT_RED if er < 20 else
                FMT_EVEN if i%2==0 else FMT_ODD)
        reqs.append(fmt(ws, f"A{i}:M{i}", base))
        reqs.append(fmt(ws, f"F{i}", FMT_MANUAL))
        reqs.append(fmt(ws, f"G{i}", FMT_MANUAL))
        reqs.append(fmt(ws, f"M{i}", FMT_MANUAL))

    last = len(posts) + 1
    reqs += [
        border(ws, f"A1:M{last}"),
        hide_cols(ws, 13, 20),
        hide_rows(ws, last, 500),
        note_req(ws,"F1","Вводи вручную: просмотры через 24 часа после публикации"),
        note_req(ws,"G1","Вводи вручную: просмотры через 72 часа после публикации"),
        note_req(ws,"M1","Вводи вручную: заметки и наблюдения по посту"),
    ]
    push(book, reqs)
    set_frozen(ws, rows=1)

def write_dashboard(ws, book, posts, subs):
    push(book,[unmerge(ws), show_cols(ws,20), show_rows(ws,500)])
    ws.clear(); time.sleep(1)

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

def write_channel(ws, book, ch, subs, desc):
    push(book,[unmerge(ws), show_cols(ws,20), show_rows(ws,500)])
    ws.clear(); time.sleep(1)
    ws.update(values=[
        ["Параметр",   "Значение",          "Обновлено"],
        ["Название",   ch.title,            datetime.now().strftime("%Y-%m-%d %H:%M")],
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

async def main():
    from telethon.sessions import StringSession
    SESSION = os.getenv("SESSION_STRING")

    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as tg:
        
        full=await tg(GetFullChannelRequest(CHANNEL))
        ch=full.chats[0]; subs=full.full_chat.participants_count
        desc=full.full_chat.about or ""
        print(f"Канал: {ch.title} | Подписчики: {subs}")

        posts=[]
        async for msg in tg.iter_messages(CHANNEL,limit=200):
            if not msg.message: continue
            views=msg.views or 0; forwards=msg.forwards or 0
            replies=(msg.replies.replies if msg.replies else 0)
            rt,rd=0,{}
            if msg.reactions:
                for r in msg.reactions.results:
                    e=r.reaction.emoticon if hasattr(r.reaction,"emoticon") else "?"
                    rd[e]=r.count; rt+=r.count
            posts.append({
                "id":msg.id,
                "date":msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "text_preview":(msg.message or "")[:80].replace("\n"," "),
                "views":views,"forwards":forwards,"replies":replies,
                "reactions_total":rt,"reactions_fmt":fmt_reactions(rd),
                "er":round(rt/views*100,1) if views else 0,
            })

        print(f"Постов: {len(posts)}")
        book=get_book()

        write_channel(get_or_create(book,"Канал"),book,ch,subs,desc)
        print("✅ Канал"); time.sleep(3)

        write_posts(get_or_create(book,"Посты"),book,posts,subs)
        print("✅ Посты"); time.sleep(3)

        write_dashboard(get_or_create(book,"Дашборд"),book,posts,subs)
        print("✅ Дашборд")

        print(f"\n📊 https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}")

if __name__=="__main__":
    asyncio.run(main())