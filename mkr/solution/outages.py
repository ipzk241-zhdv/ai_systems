import json
import re
from datetime import datetime, timedelta
from html import escape
from itertools import groupby
from urllib.parse import urlencode

from sources import KYIV, fetch, page


SCHEDULE_URL = "https://www.ztoe.com.ua/unhooking-search.php"
CURRENT_URL = "https://ztoe-poweron.inneti.net/"
API_URL = "https://api-ztoe-poweron.inneti.net/api/"
ZHYTOMYR_ID = 333
SUBGROUPS = [f"{queue}.{part}" for queue in range(1, 7) for part in (1, 2)]
TIMES = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(0, 1441, 30)]
SLOTS = [f"{start}-{end}" for start, end in zip(TIMES, TIMES[1:])]
COLORS = {"#ff3333": True, "#ff0000": True, "#f00": True, "red": True,
          "#ffffff": False, "#fff": False, "white": False}
SUBREASONS = {1: "графік аварійних відключень", 2: "графік погодинних відключень",
              3: "спеціальний графік аварійних відключень"}
TYPES = {0: "планове відключення", 1: "аварійне відключення", 2: "оперативні перемикання"}


def parse_schedule(soup):
    schedules = {}
    for table in soup.find_all("table"):
        rows = table.select(":scope > tr, :scope > tbody > tr, :scope > thead > tr")
        cells = [row.find_all(["td", "th"], recursive=False) for row in rows]
        if len(cells) < 2:
            continue
        days = [cell.get_text(strip=True) for cell in cells[0] if cell.get("colspan") == "48"]
        if not days:
            continue
        width = len(days) * len(SLOTS)
        headers = [re.sub(r"\s+", "", cell.get_text()) for cell in cells[1][-width:]]
        if headers != SLOTS * len(days):
            raise ValueError("Невідома розмітка інтервалів.")
        groups = {}
        for row in cells[2:]:
            names = [cell.get_text(strip=True) for cell in row[:-width]]
            subgroup = next((name for name in names if name in SUBGROUPS), None)
            if subgroup is None:
                continue
            if subgroup in groups:
                raise ValueError("Повторюється підчерга.")
            colors = [re.search(r"background(?:-color)?\s*:\s*([^;]+)", cell.get("style", ""), re.I)
                      for cell in row[-width:]]
            groups[subgroup] = [COLORS[color[1].strip().lower()] if color else None for color in colors]
            if None in groups[subgroup]:
                raise ValueError("Невідомий стан клітинки графіка.")
        if set(groups) != set(SUBGROUPS):
            raise ValueError("У графіку бракує підчерг.")
        for index, label in enumerate(days):
            day = datetime.strptime(label, "%d.%m.%Y").date()
            start = index * len(SLOTS)
            schedules[day] = {group: slots[start:start + len(SLOTS)] for group, slots in groups.items()}
    if not schedules:
        raise ValueError("Таблицю графіка не знайдено.")
    updated = re.search(r"Дата оновлення інформації\s*[-–:]\s*(\d{2}:\d{2}\s+\d{2}\.\d{2}\.\d{4})",
                        soup.get_text(" ", strip=True))
    return schedules, updated[1] if updated else ""


def planned_intervals(slots):
    start = 0
    intervals = []
    for active, group in groupby(slots):
        end = start + sum(1 for _ in group)
        if active:
            intervals.append(f"{TIMES[start]}–{TIMES[end]}")
        start = end
    return ", ".join(intervals) or "без позначених відключень"


def schedule_messages(today):
    link = f'<a href="{SCHEDULE_URL}">Графік і пошук підчерги за адресою</a>'
    try:
        schedules, updated = parse_schedule(page(SCHEDULE_URL))
    except (OSError, ValueError, KeyError):
        yield f"Не вдалося прочитати погодинний графік.\n{link}"
        return
    days = [day for day in (today, today + timedelta(days=1)) if day in schedules]
    if today not in schedules:
        yield f"У джерелі немає графіка на {today:%d.%m.%Y}.\n{link}"
    for day in days:
        lines = [f"<b>Погодинні відключення · {day:%d.%m.%Y}</b>"]
        if updated:
            lines.append(f"Оновлено джерелом {escape(updated)} за Києвом.")
        lines.append("")
        lines.extend(f"<b>{name}</b>  {planned_intervals(schedules[day][name])}" for name in SUBGROUPS)
        lines.extend(["", "Це інтервали за графіком. Аварійні та планові роботи можуть його змінювати.", link])
        yield "\n".join(lines)


def api_collection(resource, **params):
    result = json.loads(fetch(f"{API_URL}{resource}?{urlencode(params)}"))
    rows, total = result["hydra:member"], result["hydra:totalItems"]
    if not isinstance(rows, list) or type(total) is not int or total < len(rows):
        raise ValueError("Невідома відповідь API.")
    return rows, total


def street_matches(query):
    rows, _ = api_collection("pw_streets", pagination="false", name=query, **{"city.id": ZHYTOMYR_ID})
    exact = [row for row in rows if row["name"].casefold() == query.casefold()]
    return exact or rows


def kyiv_time(value, *, checked_at=None, switching_minutes=0):
    if value:
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                local = stamp.astimezone(KYIV).strftime("%d.%m %H:%M")
                if checked_at is not None and stamp + timedelta(minutes=switching_minutes) < checked_at:
                    return f"{local} (час минув, запис досі в джерелі)"
                if switching_minutes:
                    return f"{local} (+{switching_minutes} хв на перемикання)"
                return local
        except (ValueError, AttributeError):
            pass
    return "не вказано"


def current_message(now, street_query=None):
    link = f'<a href="{CURRENT_URL}">Діючі відключення на сайті Житомиробленерго</a>'
    scope = "Житомир"
    params = {"city.id": ZHYTOMYR_ID, "page": 1}
    try:
        if street_query:
            matches = street_matches(street_query)
            if not matches:
                return f"Не знайшов вулицю «{escape(street_query)}» у Житомирі.\n{link}"
            if len(matches) > 1:
                names = "\n".join(f"• {escape(row['name'])}" for row in matches[:8])
                return f"Знайшов кілька вулиць. Напиши <code>Відключення: повна назва</code>.\n{names}\n{link}"
            params["street.id"] = matches[0]["id"]
            scope += f" · {matches[0]['name']}"
        rows, total = api_collection("pw_accidents", **params)
        if any(row["city"]["id"] != ZHYTOMYR_ID or
               (street_query and row["street"]["id"] != params["street.id"]) for row in rows):
            raise ValueError("API не застосував фільтр адреси.")
        limit = 8 if street_query else 5
        shown = rows[:limit]
        lines = [f"<b>Діючі відключення · {escape(scope)}</b>", f"Перевірено {now:%d.%m %H:%M} за Києвом."]
        if total == 0:
            lines.append("У джерелі немає записів для цього пошуку.")
        elif len(shown) < total:
            lines.append(f"Показано {len(shown)} з {total} записів.")
        for row in shown:
            street = escape(row["street"]["name"] or "Вулиця не вказана")
            buildings = row.get("buildingNames") or "не вказано"
            if len(buildings) > 160:
                buildings = buildings[:157].rsplit(",", 1)[0] + "…"
            kind = SUBREASONS.get(row.get("switchSubReason")) or TYPES.get(row.get("typeOff"), "тип не вказано")
            started = kyiv_time(row.get("dateEvent"))
            expected = kyiv_time(row.get("datePlanIn"), checked_at=now,
                                 switching_minutes=30 if row.get("switchSubReason") == 2 else 0)
            lines.append(f"\n<b>{street}</b> · {kind}\nБудинки: {escape(buildings)}\n"
                         f"Початок {started} · очікуване ввімкнення {expected}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"Не вдалося отримати діючі відключення в Житомирі.\n{link}"
    if not street_query:
        lines.append("\nДля конкретної вулиці напиши <code>Відключення: Київська</code>.")
    lines.append(link)
    return "\n".join(lines)
