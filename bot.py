import logging
import random
import sqlite3
import datetime
import asyncio
import os
from typing import Dict, Tuple, List, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Токен бота из переменной окружения
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("No BOT_TOKEN environment variable set")

# ================== КОНСТАНТЫ ИГРЫ ==================
BASE_CLICK_REWARD = (5, 15)        # базовый диапазон золота за клик
BASE_EXP_REWARD = (1, 3)           # базовый диапазон опыта за клик
EXP_PER_LEVEL = 100                 # опыта для перехода на следующий уровень

# Улучшения (id: название, цена, эффект)
UPGRADES = {
    'click_power': {
        'name': '⚡ Сила клика',
        'description': 'Увеличивает доход за клик на +2 золота',
        'base_price': 50,
        'price_mult': 2.0,
        'effect': {'click_gold': 2}
    },
    'crit_chance': {
        'name': '🍀 Шанс крита',
        'description': 'Увеличивает шанс двойной добычи на 2%',
        'base_price': 100,
        'price_mult': 1.5,
        'effect': {'crit_chance': 2}
    },
    'auto_clicker': {
        'name': '🤖 Автокликер',
        'description': 'Каждые 10 минут приносит доход, равный 1 клику',
        'base_price': 200,
        'price_mult': 2.0,
        'effect': {'auto_income': 1}
    }
}

# Ежедневные задания (расширенный список)
DAILY_TASK_TEMPLATES = [
    {'name': 'Труженик', 'description': 'Совершить {} кликов', 'goal': (10, 30), 'reward_gold': 50, 'reward_exp': 20},
    {'name': 'Золотоискатель', 'description': 'Заработать {} золота', 'goal': (100, 500), 'reward_gold': 100, 'reward_exp': 30},
    {'name': 'Покупатель', 'description': 'Купить улучшений на {} золота', 'goal': (150, 300), 'reward_gold': 80, 'reward_exp': 25},
    {'name': 'Везунчик', 'description': 'Получить {} критических ударов', 'goal': (3, 8), 'reward_gold': 70, 'reward_exp': 40},
    {'name': 'Рудокоп', 'description': 'Добыть {} ресурсов', 'goal': (5, 15), 'reward_gold': 60, 'reward_exp': 35},
    {'name': 'Продавец', 'description': 'Продать ресурсов на {} золота', 'goal': (200, 500), 'reward_gold': 90, 'reward_exp': 45}
]

# Еженедельные задания
WEEKLY_TASK_TEMPLATES = [
    {'name': 'Шахтёр-неделя', 'description': 'Совершить {} кликов за неделю', 'goal': (200, 500), 'reward_gold': 500, 'reward_exp': 200},
    {'name': 'Золотая лихорадка', 'description': 'Заработать {} золота за неделю', 'goal': (2000, 5000), 'reward_gold': 1000, 'reward_exp': 500},
    {'name': 'Магнат', 'description': 'Купить улучшений на {} золота за неделю', 'goal': (1500, 3000), 'reward_gold': 800, 'reward_exp': 400},
    {'name': 'Критический удар', 'description': 'Получить {} критических ударов за неделю', 'goal': (20, 50), 'reward_gold': 600, 'reward_exp': 300},
    {'name': 'Коллекционер', 'description': 'Добыть {} ресурсов за неделю', 'goal': (50, 150), 'reward_gold': 700, 'reward_exp': 350},
    {'name': 'Торговец', 'description': 'Продать ресурсов на {} золота за неделю', 'goal': (2000, 5000), 'reward_gold': 900, 'reward_exp': 450}
]

# Стикеры (замените на реальные file_id)
STICKERS = {
    'crit': 'CAACAgIAAxkBAAEBuK1mM3Fhx7...',   # вставьте сюда file_id стикера для крита
    'achievement': 'CAACAgIAAxkBAAEBuK9mM3Gx8...', # для достижения
    'purchase': 'CAACAgIAAxkBAAEBuLFmM3Hx9...'     # для покупки
}

# Ресурсы
RESOURCES = {
    'coal': {'name': 'Уголь', 'base_price': 5, 'rarity': 0.5},
    'iron': {'name': 'Железо', 'base_price': 10, 'rarity': 0.3},
    'gold': {'name': 'Золотая руда', 'base_price': 30, 'rarity': 0.15},
    'diamond': {'name': 'Алмаз', 'base_price': 100, 'rarity': 0.04},
    'mithril': {'name': 'Мифрил', 'base_price': 300, 'rarity': 0.01}
}

# ================== КЛАСС ДОСТИЖЕНИЙ ==================
class Achievement:
    def __init__(self, id: str, name: str, description: str, condition_func, reward_gold: int = 0, reward_exp: int = 0):
        self.id = id
        self.name = name
        self.description = description
        self.condition_func = condition_func
        self.reward_gold = reward_gold
        self.reward_exp = reward_exp

# Функции-условия для достижений
def condition_first_click(user_id):
    stats = get_player_stats(user_id)
    achieved = stats['clicks'] >= 1
    return achieved, stats['clicks'], 1

def condition_clicks_100(user_id):
    stats = get_player_stats(user_id)
    return stats['clicks'] >= 100, stats['clicks'], 100

def condition_gold_1000(user_id):
    stats = get_player_stats(user_id)
    return stats['total_gold'] >= 1000, stats['total_gold'], 1000

def condition_crits_50(user_id):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT total_crits FROM players WHERE user_id = ?", (user_id,))
    crits = c.fetchone()[0]
    conn.close()
    return crits >= 50, crits, 50

def condition_crit_streak_5(user_id):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT max_crit_streak FROM players WHERE user_id = ?", (user_id,))
    streak = c.fetchone()[0]
    conn.close()
    return streak >= 5, streak, 5

def condition_resources_50(user_id):
    inv = get_inventory(user_id)
    total = sum(inv.values())
    return total >= 50, total, 50

ACHIEVEMENTS = [
    Achievement('first_click', 'Первый шаг', 'Сделать первый клик', condition_first_click, 10, 5),
    Achievement('clicks_100', 'Трудоголик', 'Сделать 100 кликов', condition_clicks_100, 50, 20),
    Achievement('gold_1000', 'Золотая жила', 'Добыть 1000 золота', condition_gold_1000, 100, 50),
    Achievement('crits_50', 'Критическая масса', 'Получить 50 критических ударов', condition_crits_50, 80, 30),
    Achievement('crit_streak_5', 'Везунчик', 'Достичь серии критов в 5', condition_crit_streak_5, 60, 25),
    Achievement('resources_50', 'Коллекционер', 'Собрать 50 ресурсов', condition_resources_50, 70, 35)
]

# ================== ФУНКЦИИ РАБОТЫ С БД ==================
def init_db():
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    # Таблица игроков с полем last_weekly_reset
    c.execute('''CREATE TABLE IF NOT EXISTS players
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  level INTEGER DEFAULT 1,
                  exp INTEGER DEFAULT 0,
                  gold INTEGER DEFAULT 0,
                  total_clicks INTEGER DEFAULT 0,
                  total_gold_earned INTEGER DEFAULT 0,
                  total_crits INTEGER DEFAULT 0,
                  current_crit_streak INTEGER DEFAULT 0,
                  max_crit_streak INTEGER DEFAULT 0,
                  last_daily_reset DATE,
                  last_weekly_reset DATE)''')
    # Таблица улучшений
    c.execute('''CREATE TABLE IF NOT EXISTS upgrades
                 (user_id INTEGER,
                  upgrade_id TEXT,
                  level INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, upgrade_id))''')
    # Таблица ежедневных заданий
    c.execute('''CREATE TABLE IF NOT EXISTS daily_tasks
                 (user_id INTEGER,
                  task_id INTEGER,
                  task_name TEXT,
                  description TEXT,
                  goal INTEGER,
                  progress INTEGER DEFAULT 0,
                  completed BOOLEAN DEFAULT 0,
                  reward_gold INTEGER,
                  reward_exp INTEGER,
                  date DATE,
                  PRIMARY KEY (user_id, task_id))''')
    # Таблица еженедельных заданий
    c.execute('''CREATE TABLE IF NOT EXISTS weekly_tasks
                 (user_id INTEGER,
                  task_id INTEGER,
                  task_name TEXT,
                  description TEXT,
                  goal INTEGER,
                  progress INTEGER DEFAULT 0,
                  completed BOOLEAN DEFAULT 0,
                  reward_gold INTEGER,
                  reward_exp INTEGER,
                  week TEXT,  -- в формате YYYY-WW
                  PRIMARY KEY (user_id, task_id, week))''')
    # Таблица достижений
    c.execute('''CREATE TABLE IF NOT EXISTS user_achievements
                 (user_id INTEGER,
                  achievement_id TEXT,
                  unlocked_at DATE,
                  progress INTEGER,
                  max_progress INTEGER,
                  PRIMARY KEY (user_id, achievement_id))''')
    # Таблица инвентаря
    c.execute('''CREATE TABLE IF NOT EXISTS inventory
                 (user_id INTEGER,
                  resource_id TEXT,
                  amount INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, resource_id))''')
    conn.commit()
    conn.close()

def get_player(user_id: int, username: str = None):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
    player = c.fetchone()
    if not player:
        today = datetime.date.today().isoformat()
        current_week = get_week_number()
        c.execute('''INSERT INTO players 
                     (user_id, username, last_daily_reset, last_weekly_reset) 
                     VALUES (?, ?, ?, ?)''', (user_id, username, today, current_week))
        # Начальные улучшения
        for upgrade_id in UPGRADES:
            c.execute('''INSERT INTO upgrades (user_id, upgrade_id, level) VALUES (?, ?, 0)''', (user_id, upgrade_id))
        # Начальный инвентарь
        for res_id in RESOURCES:
            c.execute('''INSERT INTO inventory (user_id, resource_id, amount) VALUES (?, ?, 0)''', (user_id, res_id))
        conn.commit()
        # Генерируем задания
        generate_daily_tasks(user_id, conn)
        generate_weekly_tasks(user_id, conn)
        conn.commit()
        c.execute("SELECT * FROM players WHERE user_id = ?", (user_id,))
        player = c.fetchone()
    conn.close()
    return player

def update_player(user_id: int, **kwargs):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    set_clause = ', '.join([f"{k} = ?" for k in kwargs])
    values = list(kwargs.values()) + [user_id]
    c.execute(f"UPDATE players SET {set_clause} WHERE user_id = ?", values)
    conn.commit()
    conn.close()

def get_upgrade_level(user_id: int, upgrade_id: str) -> int:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level FROM upgrades WHERE user_id = ? AND upgrade_id = ?", (user_id, upgrade_id))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def set_upgrade_level(user_id: int, upgrade_id: str, level: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE upgrades SET level = ? WHERE user_id = ? AND upgrade_id = ?", (level, user_id, upgrade_id))
    conn.commit()
    conn.close()

def generate_daily_tasks(user_id: int, conn=None):
    should_close = False
    if conn is None:
        conn = sqlite3.connect('game.db')
        should_close = True
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute("DELETE FROM daily_tasks WHERE user_id = ? AND date = ?", (user_id, today))
    templates = random.sample(DAILY_TASK_TEMPLATES, min(3, len(DAILY_TASK_TEMPLATES)))
    for i, tmpl in enumerate(templates):
        goal = random.randint(*tmpl['goal'])
        description = tmpl['description'].format(goal)
        c.execute('''INSERT INTO daily_tasks 
                     (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, date)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, i, tmpl['name'], description, goal, tmpl['reward_gold'], tmpl['reward_exp'], today))
    conn.commit()
    if should_close:
        conn.close()

def check_daily_reset(user_id: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT last_daily_reset FROM players WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result:
        last_reset = result[0]
        today = datetime.date.today().isoformat()
        if last_reset != today:
            generate_daily_tasks(user_id, conn)
            c.execute("UPDATE players SET last_daily_reset = ? WHERE user_id = ?", (today, user_id))
            conn.commit()
    conn.close()

def get_daily_tasks(user_id: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute('''SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp 
                 FROM daily_tasks WHERE user_id = ? AND date = ?''', (user_id, today))
    tasks = c.fetchall()
    conn.close()
    return tasks

def update_daily_task_progress(user_id: int, task_name_contains: str, progress_delta: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute('''UPDATE daily_tasks SET progress = progress + ? 
                 WHERE user_id = ? AND date = ? AND completed = 0 AND task_name LIKE ?''',
              (progress_delta, user_id, today, f'%{task_name_contains}%'))
    conn.commit()
    # Проверка выполнения
    c.execute('''SELECT task_id, goal, reward_gold, reward_exp FROM daily_tasks 
                 WHERE user_id = ? AND date = ? AND completed = 0''', (user_id, today))
    tasks = c.fetchall()
    for task_id, goal, rew_gold, rew_exp in tasks:
        c.execute('''SELECT progress FROM daily_tasks WHERE user_id = ? AND task_id = ? AND date = ?''', (user_id, task_id, today))
        progress = c.fetchone()[0]
        if progress >= goal:
            c.execute('''UPDATE daily_tasks SET completed = 1 WHERE user_id = ? AND task_id = ? AND date = ?''', (user_id, task_id, today))
            c.execute('''UPDATE players SET gold = gold + ?, exp = exp + ? WHERE user_id = ?''', (rew_gold, rew_exp, user_id))
    conn.commit()
    conn.close()

# Функции для еженедельных заданий
def get_week_number(date=None):
    if date is None:
        date = datetime.date.today()
    year, week, _ = date.isocalendar()
    return f"{year}-{week:02d}"

def generate_weekly_tasks(user_id: int, conn=None):
    should_close = False
    if conn is None:
        conn = sqlite3.connect('game.db')
        should_close = True
    c = conn.cursor()
    week = get_week_number()
    c.execute("DELETE FROM weekly_tasks WHERE user_id = ? AND week = ?", (user_id, week))
    templates = random.sample(WEEKLY_TASK_TEMPLATES, min(2, len(WEEKLY_TASK_TEMPLATES)))
    for i, tmpl in enumerate(templates):
        goal = random.randint(*tmpl['goal'])
        description = tmpl['description'].format(goal)
        c.execute('''INSERT INTO weekly_tasks 
                     (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, week)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (user_id, i, tmpl['name'], description, goal, tmpl['reward_gold'], tmpl['reward_exp'], week))
    conn.commit()
    if should_close:
        conn.close()

def check_weekly_reset(user_id: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT last_weekly_reset FROM players WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result:
        last_reset = result[0]
        current_week = get_week_number()
        if last_reset != current_week:
            generate_weekly_tasks(user_id, conn)
            c.execute("UPDATE players SET last_weekly_reset = ? WHERE user_id = ?", (current_week, user_id))
            conn.commit()
    conn.close()

def get_weekly_tasks(user_id: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    week = get_week_number()
    c.execute('''SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp 
                 FROM weekly_tasks WHERE user_id = ? AND week = ?''', (user_id, week))
    tasks = c.fetchall()
    conn.close()
    return tasks

def update_weekly_task_progress(user_id: int, task_name_contains: str, progress_delta: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    week = get_week_number()
    c.execute('''UPDATE weekly_tasks SET progress = progress + ? 
                 WHERE user_id = ? AND week = ? AND completed = 0 AND task_name LIKE ?''',
              (progress_delta, user_id, week, f'%{task_name_contains}%'))
    conn.commit()
    # Проверка выполнения
    c.execute('''SELECT task_id, goal, reward_gold, reward_exp FROM weekly_tasks 
                 WHERE user_id = ? AND week = ? AND completed = 0''', (user_id, week))
    tasks = c.fetchall()
    for task_id, goal, rew_gold, rew_exp in tasks:
        c.execute('''SELECT progress FROM weekly_tasks WHERE user_id = ? AND task_id = ? AND week = ?''', (user_id, task_id, week))
        progress = c.fetchone()[0]
        if progress >= goal:
            c.execute('''UPDATE weekly_tasks SET completed = 1 WHERE user_id = ? AND task_id = ? AND week = ?''', (user_id, task_id, week))
            c.execute('''UPDATE players SET gold = gold + ?, exp = exp + ? WHERE user_id = ?''', (rew_gold, rew_exp, user_id))
    conn.commit()
    conn.close()

def get_player_stats(user_id: int) -> Dict:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level, exp, gold, total_clicks, total_gold_earned, total_crits, current_crit_streak, max_crit_streak FROM players WHERE user_id = ?", (user_id,))
    player = c.fetchone()
    if not player:
        conn.close()
        return {}
    level, exp, gold, clicks, total_gold, total_crits, current_streak, max_streak = player
    upgrades = {}
    for uid in UPGRADES:
        c.execute("SELECT level FROM upgrades WHERE user_id = ? AND upgrade_id = ?", (user_id, uid))
        res = c.fetchone()
        upgrades[uid] = res[0] if res else 0
    conn.close()
    return {
        'level': level,
        'exp': exp,
        'exp_next': EXP_PER_LEVEL,
        'gold': gold,
        'clicks': clicks,
        'total_gold': total_gold,
        'total_crits': total_crits,
        'current_crit_streak': current_streak,
        'max_crit_streak': max_streak,
        'upgrades': upgrades
    }

def get_click_reward(user_id: int) -> Tuple[int, int, bool]:
    stats = get_player_stats(user_id)
    click_power_level = stats['upgrades']['click_power']
    crit_chance_level = stats['upgrades']['crit_chance']
    
    base_gold = random.randint(BASE_CLICK_REWARD[0], BASE_CLICK_REWARD[1])
    base_exp = random.randint(BASE_EXP_REWARD[0], BASE_EXP_REWARD[1])
    
    gold = base_gold + click_power_level * UPGRADES['click_power']['effect']['click_gold']
    crit_chance = crit_chance_level * UPGRADES['crit_chance']['effect']['crit_chance'] / 100.0
    is_crit = random.random() < crit_chance
    if is_crit:
        gold *= 2
        base_exp *= 2
    return gold, base_exp, is_crit

def level_up_if_needed(user_id: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level, exp FROM players WHERE user_id = ?", (user_id,))
    level, exp = c.fetchone()
    while exp >= EXP_PER_LEVEL:
        level += 1
        exp -= EXP_PER_LEVEL
    c.execute("UPDATE players SET level = ?, exp = ? WHERE user_id = ?", (level, exp, user_id))
    conn.commit()
    conn.close()

# ================== ФУНКЦИИ ДЛЯ РЕСУРСОВ ==================
def get_inventory(user_id: int) -> Dict[str, int]:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT resource_id, amount FROM inventory WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {res_id: amount for res_id, amount in rows}

def add_resource(user_id: int, resource_id: str, amount: int = 1):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE inventory SET amount = amount + ? WHERE user_id = ? AND resource_id = ?",
              (amount, user_id, resource_id))
    conn.commit()
    conn.close()

def remove_resource(user_id: int, resource_id: str, amount: int = 1) -> bool:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT amount FROM inventory WHERE user_id = ? AND resource_id = ?", (user_id, resource_id))
    res = c.fetchone()
    if not res or res[0] < amount:
        conn.close()
        return False
    c.execute("UPDATE inventory SET amount = amount - ? WHERE user_id = ? AND resource_id = ?",
              (amount, user_id, resource_id))
    conn.commit()
    conn.close()
    return True

# ================== ФУНКЦИИ ДЛЯ АНИМАЦИЙ И ДОСТИЖЕНИЙ ==================
async def send_animation(bot, user_id, animation_key, text=None):
    try:
        if animation_key in STICKERS:
            await bot.send_sticker(chat_id=user_id, sticker=STICKERS[animation_key])
        if text:
            await bot.send_message(chat_id=user_id, text=text)
    except Exception as e:
        logger.error(f"Failed to send animation: {e}")

async def check_achievements(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT achievement_id FROM user_achievements WHERE user_id = ?", (user_id,))
    unlocked = {row[0] for row in c.fetchall()}
    new_achievements = []
    for ach in ACHIEVEMENTS:
        if ach.id in unlocked:
            continue
        achieved, progress, max_prog = ach.condition_func(user_id)
        if achieved:
            today = datetime.date.today().isoformat()
            c.execute('''INSERT INTO user_achievements (user_id, achievement_id, unlocked_at, progress, max_progress)
                         VALUES (?, ?, ?, ?, ?)''', (user_id, ach.id, today, progress, max_prog))
            c.execute('''UPDATE players SET gold = gold + ?, exp = exp + ? WHERE user_id = ?''',
                      (ach.reward_gold, ach.reward_exp, user_id))
            new_achievements.append(ach)
    conn.commit()
    conn.close()
    for ach in new_achievements:
        text = f"🏆 Достижение получено: {ach.name}\n{ach.description}"
        if ach.reward_gold > 0 or ach.reward_exp > 0:
            text += f"\nНаграда: {ach.reward_gold}💰, {ach.reward_exp}✨"
        await send_animation(context.bot, user_id, 'achievement', text)
    return len(new_achievements)

# ================== ОБРАБОТЧИКИ КОМАНД ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_player(user.id, user.username)
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine')],
        [InlineKeyboardButton("🛒 Магазин", callback_data='shop')],
        [InlineKeyboardButton("📋 Задания", callback_data='tasks')],
        [InlineKeyboardButton("👤 Профиль", callback_data='profile')],
        [InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard')],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data='inventory')],
        [InlineKeyboardButton("💰 Рынок", callback_data='market')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Главное меню. Что хочешь сделать?"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def show_main_menu_from_query(query):
    keyboard = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine')],
        [InlineKeyboardButton("🛒 Магазин", callback_data='shop')],
        [InlineKeyboardButton("📋 Задания", callback_data='tasks')],
        [InlineKeyboardButton("👤 Профиль", callback_data='profile')],
        [InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard')],
        [InlineKeyboardButton("🎒 Инвентарь", callback_data='inventory')],
        [InlineKeyboardButton("💰 Рынок", callback_data='market')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text("Главное меню. Что хочешь сделать?", reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_main_menu_from_query: {e}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    check_daily_reset(user_id)
    check_weekly_reset(user_id)

    if data == 'mine':
        await mine_action(query, context)
    elif data == 'shop':
        await show_shop(query, context)
    elif data == 'tasks':
        await show_tasks(query, context)
    elif data == 'profile':
        await show_profile(query, context)
    elif data == 'leaderboard':
        await show_leaderboard(query, context)
    elif data == 'inventory':
        await show_inventory(query, context)
    elif data == 'market':
        await show_market(query, context)
    elif data.startswith('buy_'):
        await process_buy(query, context)
    elif data.startswith('sell_'):
        await process_sell(query, context)
    elif data == 'back_to_menu':
        await show_main_menu_from_query(query)

# ================== ОСНОВНЫЕ ДЕЙСТВИЯ ==================
async def mine_action(query, context):
    user_id = query.from_user.id
    gold, exp, is_crit = get_click_reward(user_id)
    
    # Ресурс
    rand = random.random()
    cumulative = 0
    found_resource = None
    for res_id, info in RESOURCES.items():
        cumulative += info['rarity']
        if rand < cumulative:
            found_resource = res_id
            break
    
    if found_resource:
        add_resource(user_id, found_resource, 1)
        resource_name = RESOURCES[found_resource]['name']
        resource_text = f"\nТы нашёл: {resource_name}!"
    else:
        resource_text = ""
    
    # Обновление игрока
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute('''UPDATE players SET 
                 gold = gold + ?,
                 exp = exp + ?,
                 total_clicks = total_clicks + 1,
                 total_gold_earned = total_gold_earned + ?
                 WHERE user_id = ?''', (gold, exp, gold, user_id))
    
    if is_crit:
        c.execute('''UPDATE players SET 
                     total_crits = total_crits + 1,
                     current_crit_streak = current_crit_streak + 1,
                     max_crit_streak = MAX(max_crit_streak, current_crit_streak)
                     WHERE user_id = ?''', (user_id,))
    else:
        c.execute('''UPDATE players SET current_crit_streak = 0 WHERE user_id = ?''', (user_id,))
    conn.commit()
    conn.close()
    
    level_up_if_needed(user_id)
    
    # Обновление заданий (ежедневных и еженедельных)
    update_daily_task_progress(user_id, 'Труженик', 1)
    update_daily_task_progress(user_id, 'Золотоискатель', gold)
    if is_crit:
        update_daily_task_progress(user_id, 'Везунчик', 1)
    if found_resource:
        update_daily_task_progress(user_id, 'Рудокоп', 1)
    
    update_weekly_task_progress(user_id, 'Шахтёр', 1)
    update_weekly_task_progress(user_id, 'Золотая лихорадка', gold)
    if is_crit:
        update_weekly_task_progress(user_id, 'Критический удар', 1)
    if found_resource:
        update_weekly_task_progress(user_id, 'Коллекционер', 1)
    
    if is_crit:
        await send_animation(context.bot, user_id, 'crit')
    
    await check_achievements(user_id, context)
    
    crit_text = "💥 КРИТ!" if is_crit else ""
    text = f"Ты добыл: {gold} золота {crit_text}{resource_text}\nПолучено опыта: {exp}"
    await query.message.reply_text(text)
    await show_main_menu_from_query(query)

async def show_shop(query, context):
    user_id = query.from_user.id
    stats = get_player_stats(user_id)
    gold = stats['gold']
    text = f"🛒 Магазин улучшений\nТвоё золото: {gold}\n\n"
    keyboard = []
    for upgrade_id, info in UPGRADES.items():
        level = stats['upgrades'][upgrade_id]
        price = int(info['base_price'] * (info['price_mult'] ** level))
        text += f"{info['name']} (ур. {level})\n{info['description']}\nЦена следующего уровня: {price} золота\n\n"
        keyboard.append([InlineKeyboardButton(f"Купить {info['name']} за {price}", callback_data=f'buy_{upgrade_id}')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_shop: {e}")

async def process_buy(query, context):
    upgrade_id = query.data.replace('buy_', '')
    user_id = query.from_user.id
    stats = get_player_stats(user_id)
    level = stats['upgrades'][upgrade_id]
    price = int(UPGRADES[upgrade_id]['base_price'] * (UPGRADES[upgrade_id]['price_mult'] ** level))
    
    if stats['gold'] < price:
        await query.edit_message_text("❌ Недостаточно золота! Пополни баланс кликами.")
        return
    
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET gold = gold - ? WHERE user_id = ?", (price, user_id))
    c.execute("UPDATE upgrades SET level = level + 1 WHERE user_id = ? AND upgrade_id = ?", (user_id, upgrade_id))
    conn.commit()
    conn.close()
    
    # Обновление заданий
    update_daily_task_progress(user_id, 'Покупатель', price)
    update_weekly_task_progress(user_id, 'Магнат', price)
    
    await send_animation(context.bot, user_id, 'purchase', f"✅ {UPGRADES[upgrade_id]['name']} улучшен до {level+1} уровня.")
    await check_achievements(user_id, context)
    await show_shop(query, context)

async def show_tasks(query, context):
    user_id = query.from_user.id
    daily = get_daily_tasks(user_id)
    weekly = get_weekly_tasks(user_id)
    
    text = "📋 **Ежедневные задания:**\n"
    if daily:
        for task in daily:
            task_id, name, desc, goal, progress, completed, rew_gold, rew_exp = task
            status = "✅" if completed else f"{progress}/{goal}"
            text += f"{name}: {desc}\nПрогресс: {status}\nНаграда: {rew_gold}💰, {rew_exp}✨\n\n"
    else:
        text += "Нет заданий на сегодня.\n\n"
    
    text += "📅 **Еженедельные задания:**\n"
    if weekly:
        for task in weekly:
            task_id, name, desc, goal, progress, completed, rew_gold, rew_exp = task
            status = "✅" if completed else f"{progress}/{goal}"
            text += f"{name}: {desc}\nПрогресс: {status}\nНаграда: {rew_gold}💰, {rew_exp}✨\n\n"
    else:
        text += "Нет заданий на эту неделю.\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_tasks: {e}")

async def show_profile(query, context):
    user_id = query.from_user.id
    stats = get_player_stats(user_id)
    if not stats:
        await query.edit_message_text("Профиль не найден.")
        return
    text = (
        f"👤 Профиль игрока\n"
        f"Уровень: {stats['level']}\n"
        f"Опыт: {stats['exp']}/{stats['exp_next']}\n"
        f"Золото: {stats['gold']}\n"
        f"Всего кликов: {stats['clicks']}\n"
        f"Всего добыто золота: {stats['total_gold']}\n"
        f"Критические удары: {stats['total_crits']}\n"
        f"Макс. серия критов: {stats['max_crit_streak']}\n\n"
        f"⚡ Сила клика: ур. {stats['upgrades']['click_power']}\n"
        f"🍀 Шанс крита: ур. {stats['upgrades']['crit_chance']}\n"
        f"🤖 Автокликер: ур. {stats['upgrades']['auto_clicker']}\n"
    )
    
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute('''SELECT achievement_id, unlocked_at FROM user_achievements WHERE user_id = ? ORDER BY unlocked_at DESC LIMIT 5''', (user_id,))
    recent = c.fetchall()
    conn.close()
    if recent:
        text += "\n🏅 Последние достижения:\n"
        for ach_id, date in recent:
            ach = next((a for a in ACHIEVEMENTS if a.id == ach_id), None)
            if ach:
                text += f"• {ach.name} ({date})\n"
    else:
        text += "\nДостижений пока нет. Кликай больше!"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_profile: {e}")

async def show_leaderboard(query, context):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute('''SELECT username, level, exp FROM players ORDER BY level DESC, exp DESC LIMIT 5''')
    top_level = c.fetchall()
    c.execute('''SELECT username, gold FROM players ORDER BY gold DESC LIMIT 5''')
    top_gold = c.fetchall()
    conn.close()
    
    text = "🏆 Таблица лидеров\n\nПо уровню:\n"
    for i, (name, lvl, exp) in enumerate(top_level, 1):
        text += f"{i}. {name or 'Аноним'} — уровень {lvl} (опыт {exp})\n"
    text += "\nПо золоту:\n"
    for i, (name, gold) in enumerate(top_gold, 1):
        text += f"{i}. {name or 'Аноним'} — {gold}💰\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_leaderboard: {e}")

# ================== ИНВЕНТАРЬ И РЫНОК ==================
async def show_inventory(query, context):
    user_id = query.from_user.id
    inv = get_inventory(user_id)
    text = "🎒 **Твой инвентарь:**\n\n"
    has_items = False
    for res_id, info in RESOURCES.items():
        amount = inv.get(res_id, 0)
        if amount > 0:
            text += f"• {info['name']}: {amount} шт.\n"
            has_items = True
    if not has_items:
        text = "🎒 Твой инвентарь пуст. Добывай ресурсы!"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_inventory: {e}")

async def show_market(query, context):
    user_id = query.from_user.id
    inv = get_inventory(user_id)
    text = "💰 **Рынок ресурсов**\n\n"
    keyboard = []
    for res_id, info in RESOURCES.items():
        amount = inv.get(res_id, 0)
        price = info['base_price']
        text += f"**{info['name']}**: {amount} шт. | Цена: {price}💰 за шт.\n"
        if amount > 0:
            row = [
                InlineKeyboardButton(f"Продать 1", callback_data=f'sell_{res_id}_1'),
                InlineKeyboardButton(f"Продать всё", callback_data=f'sell_{res_id}_all')
            ]
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_market: {e}")

async def process_sell(query, context):
    data = query.data
    parts = data.split('_')  # ['sell', 'coal', '1'] или ['sell', 'coal', 'all']
    res_id = parts[1]
    sell_type = parts[2]
    user_id = query.from_user.id
    
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT amount FROM inventory WHERE user_id = ? AND resource_id = ?", (user_id, res_id))
    result = c.fetchone()
    if not result or result[0] == 0:
        await query.answer("У тебя нет этого ресурса!", show_alert=True)
        conn.close()
        return
    
    available = result[0]
    sell_qty = available if sell_type == 'all' else 1
    price_per_unit = RESOURCES[res_id]['base_price']
    total_gold = sell_qty * price_per_unit
    
    c.execute("UPDATE inventory SET amount = amount - ? WHERE user_id = ? AND resource_id = ?", (sell_qty, user_id, res_id))
    c.execute("UPDATE players SET gold = gold + ? WHERE user_id = ?", (total_gold, user_id))
    conn.commit()
    conn.close()
    
    # Обновление заданий
    update_daily_task_progress(user_id, 'Продавец', total_gold)
    update_weekly_task_progress(user_id, 'Торговец', total_gold)
    
    await query.answer(f"✅ Продано {sell_qty} {RESOURCES[res_id]['name']} за {total_gold} золота!", show_alert=False)
    await show_market(query, context)

# ================== ФУНКЦИИ ДЛЯ ВЕБ-СЕРВЕРА ==================
async def run_bot():
    logger.info("Starting bot polling...")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot polling started successfully")
        while True:
            await asyncio.sleep(10)
    except Exception as e:
        logger.error(f"Error in bot polling: {e}", exc_info=True)

async def healthcheck(request):
    return JSONResponse({"status": "alive"})

async def startup_event():
    logger.info("Starting up...")
    init_db()
    asyncio.create_task(run_bot())

async def shutdown_event():
    logger.info("Shutting down...")

app = Starlette(
    routes=[
        Route("/healthcheck", healthcheck),
        Route("/", healthcheck),
    ],
    on_startup=[startup_event],
    on_shutdown=[shutdown_event]
)

def main():
    init_db()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
