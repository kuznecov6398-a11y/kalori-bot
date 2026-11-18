import asyncio
import base64
import json
import os
import re
import sqlite3
from datetime import date
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "qwen/qwen2.5-vl-32b-instruct")
DB_PATH = Path("foodbot.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("Заполни BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
ai = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None

kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📸 Добавить фото еды"), KeyboardButton(text="✍️ Записать еду")],
        [KeyboardButton(text="📊 Мой день"), KeyboardButton(text="⚖️ Мой вес")],
        [KeyboardButton(text="🎯 Моя цель"), KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
)

class Profile(StatesGroup):
    sex = State()
    age = State()
    height = State()
    weight = State()
    goal = State()

class ManualMeal(StatesGroup):
    description = State()

class WeightInput(StatesGroup):
    value = State()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, sex TEXT, age INTEGER, height REAL,
            weight REAL, goal TEXT, kcal INTEGER, protein INTEGER, fat INTEGER, carbs INTEGER
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS meals(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, day TEXT,
            title TEXT, grams INTEGER, kcal REAL, protein REAL, fat REAL, carbs REAL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS weights(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, day TEXT, weight REAL
        )""")

def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def calculate_targets(sex, age, height, weight, goal):
    # Mifflin-St Jeor; activity multiplier 1.4 as a cautious initial estimate.
    s = 5 if sex == "Мужчина" else -161
    bmr = 10 * weight + 6.25 * height - 5 * age + s
    maintenance = bmr * 1.4
    kcal = maintenance - 400 if goal == "Похудеть" else maintenance + 250 if goal == "Набрать вес" else maintenance
    kcal = max(1200 if sex == "Женщина" else 1500, round(kcal / 50) * 50)
    protein = round(weight * (1.6 if goal == "Похудеть" else 1.4))
    fat = round(weight * 0.8)
    carbs = max(0, round((kcal - protein*4 - fat*9)/4))
    return int(kcal), protein, fat, carbs

def add_meal(uid, meal):
    with db() as con:
        con.execute("""INSERT INTO meals(user_id,day,title,grams,kcal,protein,fat,carbs)
            VALUES(?,?,?,?,?,?,?,?)""",
            (uid, date.today().isoformat(), meal["title"], meal["grams"],
             meal["kcal"], meal["protein"], meal["fat"], meal["carbs"]))

def totals(uid):
    with db() as con:
        row = con.execute("""SELECT COUNT(*) n, COALESCE(SUM(kcal),0) kcal,
            COALESCE(SUM(protein),0) protein, COALESCE(SUM(fat),0) fat,
            COALESCE(SUM(carbs),0) carbs FROM meals WHERE user_id=? AND day=?""",
            (uid, date.today().isoformat())).fetchone()
    return row

async def analyze_food(text=None, image_data_url=None):
    if not ai:
        raise RuntimeError("Распознавание пока не подключено: нужен AI_API_KEY в .env.")
    prompt = """Оцени съеденную еду по фото или описанию. Верни ТОЛЬКО JSON:
{"title":"краткое название на русском","grams":число,
"kcal":число,"protein":число,"fat":число,"carbs":число,
"confidence":"низкая|средняя|высокая","assumptions":"что предположено"}
Числа — оценка на всю указанную порцию. Не выдумывай точность: если размер порции неясен, оцени типичную порцию и укажи это в assumptions. Не давай медицинских советов."""
    content = [{"type":"text","text": prompt + (("\nОписание: "+text) if text else "")}]
    if image_data_url:
        content.append({"type":"image_url","image_url":{"url":image_data_url}})
    response = await ai.chat.completions.create(
        model=AI_MODEL, temperature=0.1,
        messages=[{"role":"user","content":content}],
    )
    raw = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("Не удалось разобрать ответ модели. Попробуй другое фото.")
    data = json.loads(match.group(0))
    for k in ("grams","kcal","protein","fat","carbs"):
        data[k] = max(0, float(data[k]))
    data["grams"] = int(data["grams"])
    return data

def meal_card(m):
    return (f"🍽 <b>{m['title']}</b>\n"
            f"Порция: ~{m['grams']} г\n\n"
            f"🔥 <b>{round(m['kcal'])} ккал</b>\n"
            f"🥩 Белки: {round(m['protein'])} г\n"
            f"🧈 Жиры: {round(m['fat'])} г\n"
            f"🍞 Углеводы: {round(m['carbs'])} г\n\n"
            f"Уверенность: {m.get('confidence','средняя')}\n"
            f"Оценка приблизительная. {m.get('assumptions','')}")

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    init_db()
    user = get_user(message.from_user.id)
    if user:
        await message.answer("С возвращением! Отправь фото еды или выбери действие в меню.", reply_markup=kb)
        return
    await state.set_state(Profile.sex)
    await message.answer("Привет! Я помогу вести дневник питания и считать калории.\n\nДля начала выбери пол: Мужчина или Женщина.")

@dp.message(Profile.sex)
async def set_sex(message: Message, state: FSMContext):
    val = message.text.strip()
    if val not in ("Мужчина", "Женщина"):
        await message.answer("Напиши: Мужчина или Женщина.")
        return
    await state.update_data(sex=val)
    await state.set_state(Profile.age)
    await message.answer("Сколько тебе полных лет?")

@dp.message(Profile.age)
async def set_age(message: Message, state: FSMContext):
    try: val=int(message.text)
    except: val=0
    if not 14 <= val <= 100:
        await message.answer("Укажи возраст числом от 14 до 100.")
        return
    await state.update_data(age=val); await state.set_state(Profile.height)
    await message.answer("Укажи рост в сантиметрах, например 176.")

@dp.message(Profile.height)
async def set_height(message: Message, state: FSMContext):
    try: val=float(message.text.replace(",", "."))
    except: val=0
    if not 120 <= val <= 230:
        await message.answer("Укажи рост от 120 до 230 см.")
        return
    await state.update_data(height=val); await state.set_state(Profile.weight)
    await message.answer("Укажи текущий вес в кг, например 95.")

@dp.message(Profile.weight)
async def set_weight(message: Message, state: FSMContext):
    try: val=float(message.text.replace(",", "."))
    except: val=0
    if not 30 <= val <= 300:
        await message.answer("Укажи вес от 30 до 300 кг.")
        return
    await state.update_data(weight=val); await state.set_state(Profile.goal)
    await message.answer("Какая цель? Напиши: Похудеть, Поддерживать или Набрать вес.")

@dp.message(Profile.goal)
async def set_goal(message: Message, state: FSMContext):
    goal=message.text.strip()
    if goal not in ("Похудеть","Поддерживать","Набрать вес"):
        await message.answer("Выбери: Похудеть, Поддерживать или Набрать вес.")
        return
    d=await state.get_data()
    kcal,p,f,c=calculate_targets(d["sex"],d["age"],d["height"],d["weight"],goal)
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO users
            (user_id,sex,age,height,weight,goal,kcal,protein,fat,carbs)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (message.from_user.id,d["sex"],d["age"],d["height"],d["weight"],goal,kcal,p,f,c))
        con.execute("INSERT INTO weights(user_id,day,weight) VALUES(?,?,?)",
                    (message.from_user.id,date.today().isoformat(),d["weight"]))
    await state.clear()
    await message.answer(
        f"Профиль готов! 🎯\nТвоя ориентировочная цель: <b>{kcal} ккал/день</b>\n"
        f"Белки: {p} г · Жиры: {f} г · Углеводы: {c} г\n\n"
        "Теперь отправь фото еды или опиши, что съел.", reply_markup=kb)

@dp.message(F.photo)
async def photo_meal(message: Message):
    user=get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала настрой профиль командой /start."); return
    if not ai:
        await message.answer("Фото получил. Для автоматического распознавания нужно подключить AI API key в настройках бота.")
        return
    wait=await message.answer("🔎 Рассматриваю фото и оцениваю порцию…")
    try:
        photo=message.photo[-1]
        file=await bot.get_file(photo.file_id)
        data=await bot.download_file(file.file_path)
        encoded=base64.b64encode(data.read()).decode()
        caption=message.caption or ""
        m=await analyze_food(caption, f"data:image/jpeg;base64,{encoded}")
        await state_save_and_offer(message, wait, m)
    except Exception as e:
        await wait.edit_text(f"Не получилось распознать фото: {str(e)[:250]}")

async def state_save_and_offer(message, wait, m):
    add_meal(message.from_user.id,m)
    t=totals(message.from_user.id); u=get_user(message.from_user.id)
    await wait.edit_text(meal_card(m)+f"\n\n📊 За сегодня: {round(t['kcal'])}/{u['kcal']} ккал", parse_mode="HTML")

@dp.message(F.text == "📸 Добавить фото еды")
async def ask_photo(message: Message):
    await message.answer("Просто отправь фотографию блюда сюда. Можно добавить подпись с составом или весом.")

@dp.message(F.text == "✍️ Записать еду")
async def ask_text(message: Message, state: FSMContext):
    await state.set_state(ManualMeal.description)
    await message.answer("Напиши, что и примерно сколько ты съел. Например: 2 яйца, 2 кусочка хлеба и 30 г сыра.")

@dp.message(ManualMeal.description)
async def parse_text_meal(message: Message, state: FSMContext):
    try:
        m=await analyze_food(text=message.text)
        add_meal(message.from_user.id,m)
        t=totals(message.from_user.id); u=get_user(message.from_user.id)
        await message.answer(meal_card(m)+f"\n\n📊 За сегодня: {round(t['kcal'])}/{u['kcal']} ккал", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await message.answer(f"Не получилось рассчитать: {str(e)[:250]}")
    await state.clear()

@dp.message(Command("today"))
@dp.message(F.text == "📊 Мой день")
async def today(message: Message):
    u=get_user(message.from_user.id)
    if not u: await message.answer("Сначала /start"); return
    t=totals(message.from_user.id)
    left=max(0,round(u["kcal"]-t["kcal"]))
    await message.answer(
        f"📊 <b>Твой день</b>\n🔥 {round(t['kcal'])}/{u['kcal']} ккал\n"
        f"Осталось: {left} ккал\n\n"
        f"🥩 Белки: {round(t['protein'])}/{u['protein']} г\n"
        f"🧈 Жиры: {round(t['fat'])}/{u['fat']} г\n"
        f"🍞 Углеводы: {round(t['carbs'])}/{u['carbs']} г\n"
        f"🍽 Приёмов пищи: {t['n']}", parse_mode="HTML", reply_markup=kb)

@dp.message(Command("weight"))
@dp.message(F.text == "⚖️ Мой вес")
async def weight(message: Message, state: FSMContext):
    await state.set_state(WeightInput.value)
    await message.answer("Напиши текущий вес в кг, например 92.4.")

@dp.message(WeightInput.value)
async def save_weight(message: Message, state: FSMContext):
    try: val=float(message.text.replace(",", "."))
    except: val=0
    if not 30 <= val <= 300:
        await message.answer("Введи вес числом от 30 до 300 кг."); return
    with db() as con:
        con.execute("INSERT INTO weights(user_id,day,weight) VALUES(?,?,?)",
                    (message.from_user.id,date.today().isoformat(),val))
        con.execute("UPDATE users SET weight=? WHERE user_id=?", (val,message.from_user.id))
    await state.clear()
    await message.answer(f"Вес {val:.1f} кг записан ✅", reply_markup=kb)

@dp.message(Command("goal"))
@dp.message(F.text == "🎯 Моя цель")
async def goal(message: Message):
    u=get_user(message.from_user.id)
    if not u: await message.answer("Сначала /start"); return
    await message.answer(f"Цель: {u['goal']}\nНорма: {u['kcal']} ккал\nБ/Ж/У: {u['protein']}/{u['fat']}/{u['carbs']} г")

@dp.message(Command("help"))
@dp.message(F.text == "❓ Помощь")
async def help_msg(message: Message):
    await message.answer("/start — профиль\n/today — итог за день\n/weight — записать вес\n/goal — цель и нормы\n\nОтправь фото еды или опиши приём пищи текстом.", reply_markup=kb)

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
