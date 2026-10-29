"""
Этап 2: фраза -> Gemini -> тренировка в Garmin Connect.

Установка:  py -m pip install -U google-genai garminconnect
Запуск:     py garmin_workout.py

Ключ Gemini берётся из переменной окружения GEMINI_API_KEY.
Если её нет, скрипт спросит ключ при запуске (в файл его не вписываем!).
"""

import getpass
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from garminconnect import Garmin
from google import genai
from google.genai import errors, types

# Название модели Gemini. Если перестанет работать, возьми актуальное в AI Studio.
GEMINI_MODEL = "gemini-3.5-flash"
# Запасная модель на случай, если основная перегружена
GEMINI_FALLBACK_MODEL = "gemini-3.1-flash-lite"

TOKENSTORE = str(Path.home() / ".garminconnect")
RUNNING = {"sportTypeId": 1, "sportTypeKey": "running"}

# ПАНО (лактатный порог) по Garmin, уд/мин. Обновляй цифру, когда она изменится.
LTHR = 169

# Границы зон Garmin в процентах от ПАНО (из настроек твоих часов)
ZONES_PCT = {1: (57, 67), 2: (68, 78), 3: (79, 90), 4: (91, 101), 5: (102, 114)}

# Если назван один темп (например 5:00), коридор будет +- столько секунд на километр
PACE_TOLERANCE_S = 5

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM_PROMPT = """Ты превращаешь описание беговой тренировки в JSON. Отвечай ТОЛЬКО JSON, без пояснений.

Формат:
{
  "name": "короткое название тренировки",
  "date": "YYYY-MM-DD",
  "items": [
    {"kind": "step", "type": "warmup|interval|recovery|cooldown",
     "distance_m": число или null, "duration_s": число или null, "hr_zone": 1-5 или null,
     "pano_pct": число или [мин, макс] или null,
     "pace_s_per_km": число или [мин, макс] или null},
    {"kind": "repeat", "repeats": число, "steps": [ ...шаги как выше, kind="step"... ]}
  ],
  "question": null
}

Правила:
- Порядок items = порядок в тренировке: разминка, потом основная часть, потом заминка.
- "работа" это type interval, "отдых" это type recovery.
- Дистанция всегда в метрах (1 км = 1000), время в секундах (5 минут = 300).
- В каждом шаге заполняй либо distance_m, либо duration_s, второе поле null.
- hr_zone это номер зоны пульса, если зона названа прямо ("4 зона"), иначе null.
- Если интенсивность дана в процентах от ПАНО ("85% от ПАНО", "80-85% ПАНО"), положи процент в pano_pct (число или массив [мин, макс]), а hr_zone оставь null. Зону сам не вычисляй, это сделает программа.
- Если процентов ПАНО нет, pano_pct = null.
- Непрерывный бег без разминки и заминки ("бег 40 минут") это один шаг type interval.
- Если задан темп бега ("5 мин/км", "в темпе 5:10", "темп 6.3"), положи его в pace_s_per_km в СЕКУНДАХ на километр (5:00 = 300) как число, либо как [мин, макс] для диапазона ("5:00-5:10" = [300, 310]). Для такого шага hr_zone и pano_pct оставь null.
- Темп с точкой или запятой читай как минуты.секунды, а одна цифра после точки это десятки секунд: 6.3 = 6:30 = 390, 5.45 = 5:45 = 345, 6.30 = 6:30 = 390.
- Если для одного шага названы и темп, и пульс (зона, % ПАНО), в шаге может быть только одна цель: поставь в "question" вопрос, что использовать.
- Даты: "завтра", "в пятницу" и т.п. переводи в YYYY-MM-DD относительно сегодняшней даты, которую я сообщу.
- Если для тренировки не хватает важных данных (не ясно число повторов, дистанция или время шага, дата), поставь в "question" короткий вопрос на русском, а остальное заполни как смог. Если всё понятно, "question" = null.
- Ничего не выдумывай: если разминка не упомянута, не добавляй её.
"""


# ---------- Gemini ----------

def get_gemini_client():
    key = os.environ.get("GEMINI_API_KEY") or getpass.getpass("Ключ Gemini API: ").strip()
    return genai.Client(api_key=key)


def get_today():
    """Сегодняшняя дата. На сервере задай TZ_OFFSET_HOURS (например 5),
    чтобы \"завтра\" считалось по твоему времени, а не по времени сервера."""
    offset = os.environ.get("TZ_OFFSET_HOURS")
    if offset:
        return datetime.now(timezone(timedelta(hours=float(offset)))).date()
    return date.today()


def parse_workout(client, text):
    today = get_today()
    prompt = f"Сегодня {today.isoformat()} ({WEEKDAYS[today.weekday()]}).\nОписание тренировки: {text}"
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
    )

    # Сервер Gemini иногда перегружен (ошибка 503). Пробуем несколько раз,
    # потом переключаемся на запасную модель.
    last_error = None
    for model in (GEMINI_MODEL, GEMINI_FALLBACK_MODEL):
        for attempt in range(3):
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                return json.loads(resp.text)
            except errors.ServerError as e:
                last_error = e
                wait = 3 * (attempt + 1)
                print(f"Gemini ({model}) сейчас перегружен, повтор через {wait} с...")
                time.sleep(wait)
        print(f"Модель {model} не отвечает, пробую запасную...")
    raise last_error


# ---------- Проценты от ПАНО -> зоны Garmin ----------

def pct_to_zone(pct):
    """Процент от ПАНО -> номер зоны Garmin (по нижним границам зон)."""
    for zone in (5, 4, 3, 2):
        if pct >= ZONES_PCT[zone][0]:
            return zone
    return 1


def apply_pano(data):
    """Для шагов с pano_pct считает зону Garmin и добавляет пояснение с расчётом."""

    def handle(s):
        p = s.get("pano_pct")
        if p is None or s.get("hr_zone"):
            return
        if isinstance(p, (list, tuple)):
            lo, hi = float(min(p)), float(max(p))
        else:
            lo = hi = float(p)

        mid = (lo + hi) / 2
        zone = pct_to_zone(mid)
        s["hr_zone"] = zone

        if lo != hi:
            pct_txt = f"{lo:g}-{hi:g}%"
            bpm_txt = f"{round(lo * LTHR / 100)}-{round(hi * LTHR / 100)}"
        else:
            pct_txt = f"{lo:g}%"
            bpm_txt = f"{round(lo * LTHR / 100)}"

        zlo, zhi = ZONES_PCT[zone]
        note = f"{pct_txt} от ПАНО {LTHR} = {bpm_txt} уд/мин, зона {zone} ({zlo}-{zhi}%)"
        if pct_to_zone(lo) != pct_to_zone(hi):
            note += f"; ВНИМАНИЕ: диапазон захватывает зоны {pct_to_zone(lo)}-{pct_to_zone(hi)}"
        if mid < ZONES_PCT[1][0]:
            note += "; это ниже зоны 1"
        if mid > ZONES_PCT[5][1]:
            note += "; это выше зоны 5"
        s["note"] = note
        # Короткая заметка, которая попадёт в сам шаг тренировки в Garmin
        s["garmin_note"] = f"{bpm_txt} bpm"

    for item in data["items"]:
        if item["kind"] == "repeat":
            for s in item["steps"]:
                handle(s)
        else:
            handle(item)


# ---------- Темп ----------

def fmt_pace(sec):
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def apply_pace(data):
    """Для шагов с pace_s_per_km считает коридор темпа и добавляет пояснение."""

    def handle(s):
        p = s.get("pace_s_per_km")
        if p is None:
            return
        if isinstance(p, (list, tuple)):
            slow, fast = float(max(p)), float(min(p))
            note = f"темп {fmt_pace(fast)}-{fmt_pace(slow)} мин/км"
        else:
            slow, fast = float(p) + PACE_TOLERANCE_S, float(p) - PACE_TOLERANCE_S
            note = (f"темп {fmt_pace(p)} мин/км, коридор "
                    f"{fmt_pace(fast)}-{fmt_pace(slow)}")
        s["pace_range"] = (slow, fast)
        if s.get("hr_zone") or s.get("pano_pct") is not None:
            note += "; пульс как цель не используется, только темп"
            s["hr_zone"] = None
            s["pano_pct"] = None
        s["note"] = note

    for item in data["items"]:
        if item["kind"] == "repeat":
            for s in item["steps"]:
                handle(s)
        else:
            handle(item)


# ---------- Показ расшифровки ----------

TYPE_RU = {"warmup": "Разминка", "interval": "Работа", "recovery": "Отдых", "cooldown": "Заминка"}


def describe_step(s):
    if s.get("distance_m"):
        size = f"{s['distance_m']} м"
    elif s.get("duration_s"):
        sec = s["duration_s"]
        size = f"{sec // 60} мин" + (f" {sec % 60} с" if sec % 60 else "")
    else:
        size = "по кнопке"
    zone = f", зона пульса {s['hr_zone']}" if s.get("hr_zone") else ""
    note = f"  [{s['note']}]" if s.get("note") else ""
    return f"{TYPE_RU.get(s['type'], s['type'])}: {size}{zone}{note}"


def summary(data):
    lines = [f"Тренировка: {data['name']}", f"Дата: {data['date']}", ""]
    for item in data["items"]:
        if item["kind"] == "repeat":
            lines.append(f"{item['repeats']} повторов:")
            for s in item["steps"]:
                lines.append(f"    {describe_step(s)}")
        else:
            lines.append(describe_step(item))
    return "\n".join(lines)


# ---------- Сборка тренировки для Garmin ----------

STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5}
NO_TARGET = {"targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"}}


def garmin_step(s, order, child=None):
    type_key = s["type"]
    if s.get("distance_m"):
        end = {"conditionTypeId": 3, "conditionTypeKey": "distance"}
        value = float(s["distance_m"])
    elif s.get("duration_s"):
        end = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        value = float(s["duration_s"])
    else:
        end = {"conditionTypeId": 1, "conditionTypeKey": "lap.button"}
        value = None

    if s.get("pace_range"):
        slow, fast = s["pace_range"]
        # Garmin хранит темп как скорость в м/с: valueOne медленнее, valueTwo быстрее
        target = {
            "targetType": {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone"},
            "targetValueOne": 1000.0 / slow,
            "targetValueTwo": 1000.0 / fast,
        }
    elif s.get("hr_zone"):
        target = {
            "targetType": {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone"},
            "zoneNumber": s["hr_zone"],
        }
    else:
        target = NO_TARGET

    out = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": STEP_TYPES[type_key], "stepTypeKey": type_key},
        "endCondition": end,
        **target,
    }
    if value is not None:
        out["endConditionValue"] = value
    if s.get("garmin_note"):
        out["description"] = s["garmin_note"]
    if child is not None:
        out["childStepId"] = child
    return out


def build_garmin_workout(data):
    steps = []
    order = 1
    for item in data["items"]:
        if item["kind"] == "repeat":
            children = [garmin_step(s, i + 1, child=1) for i, s in enumerate(item["steps"])]
            steps.append(
                {
                    "type": "RepeatGroupDTO",
                    "stepOrder": order,
                    "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
                    "numberOfIterations": item["repeats"],
                    "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
                    "endConditionValue": float(item["repeats"]),
                    "smartRepeat": False,
                    "childStepId": 1,
                    "workoutSteps": children,
                }
            )
        else:
            steps.append(garmin_step(item, order))
        order += 1

    return {
        "workoutName": data["name"],
        "sportType": RUNNING,
        "workoutSegments": [{"segmentOrder": 1, "sportType": RUNNING, "workoutSteps": steps}],
    }


# ---------- Garmin ----------

def garmin_login():
    mfa = lambda: input("Код двухфакторной защиты: ").strip()

    # Серверный режим (Render): вход по логину и паролю из переменных окружения, без вопросов
    server_email = os.environ.get("GARMIN_EMAIL")
    server_password = os.environ.get("GARMIN_PASSWORD")
    if server_email and server_password:
        client = Garmin(server_email, server_password)
        client.login()
        return client

    try:
        client = Garmin(prompt_mfa=mfa)
        client.login(TOKENSTORE)  # используем сохранённые токены
        return client
    except Exception:
        print("Сохранённый вход не сработал, нужен email и пароль.")
        email = input("Email от Garmin: ").strip()
        password = input("Пароль от Garmin: ")
        client = Garmin(email, password, prompt_mfa=mfa)
        client.login(TOKENSTORE)
        return client


# ---------- Основной сценарий ----------

def main():
    gemini = get_gemini_client()

    request = input("Опиши тренировку: ").strip()
    while True:
        data = parse_workout(gemini, request)

        if data.get("question"):
            print(f"\nУточнение: {data['question']}")
            request += ". " + input("> ").strip()
            continue

        apply_pace(data)
        apply_pano(data)
        print("\n" + summary(data) + "\n")
        answer = input("Создать? (да / или напиши, что изменить): ").strip()
        if answer.lower() in ("да", "д", "yes", "y", "ок", "ok"):
            break
        request += ". Исправление: " + answer

    client = garmin_login()
    result = client.upload_workout(build_garmin_workout(data))
    workout_id = result["workoutId"]
    client.schedule_workout(workout_id, data["date"])
    print(f"\nГотово! Тренировка создана и поставлена на {data['date']}.")


if __name__ == "__main__":
    main()
