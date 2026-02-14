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

# -------------------- Настройка логирования --------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------- Токен из переменной окружения --------------------
TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("No BOT_TOKEN environment variable set")

# ==================== КОНСТАНТЫ ИГРЫ ====================
BASE_CLICK_REWARD = (5, 15)          # базовое золото за клик
BASE_EXP_REWARD = (1, 3)             # базовый опыт за клик
EXP_PER_LEVEL = 100                   # опыт для перехода на следующий уровень

# -------------------- Улучшения персонажа (пассивные) --------------------
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

# -------------------- Ежедневные задания --------------------
DAILY_TASK_TEMPLATES = [
    {'name': 'Труженик', 'description': 'Совершить {} кликов', 'goal': (10, 30), 'reward_gold': 50, 'reward_exp': 20},
    {'name': 'Золотоискатель', 'description': 'Заработать {} золота', 'goal': (100, 500), 'reward_gold': 100, 'reward_exp': 30},
    {'name': 'Покупатель', 'description': 'Купить улучшений на {} золота', 'goal': (150, 300), 'reward_gold': 80, 'reward_exp': 25},
    {'name': 'Везунчик', 'description': 'Получить {} критических ударов', 'goal': (3, 8), 'reward_gold': 70, 'reward_exp': 40},
    {'name': 'Рудокоп', 'description': 'Добыть {} ресурсов', 'goal': (5, 15), 'reward_gold': 60, 'reward_exp': 35},
    {'name': 'Продавец', 'description': 'Продать ресурсов на {} золота', 'goal': (200, 500), 'reward_gold': 90, 'reward_exp': 45}
]

# -------------------- Еженедельные задания --------------------
WEEKLY_TASK_TEMPLATES = [
    {'name': 'Шахтёр-неделя', 'description': 'Совершить {} кликов за неделю', 'goal': (200, 500), 'reward_gold': 500, 'reward_exp': 200},
    {'name': 'Золотая лихорадка', 'description': 'Заработать {} золота за неделю', 'goal': (2000, 5000), 'reward_gold': 1000, 'reward_exp': 500},
    {'name': 'Магнат', 'description': 'Купить улучшений на {} золота за неделю', 'goal': (1500, 3000), 'reward_gold': 800, 'reward_exp': 400},
    {'name': 'Критический удар', 'description': 'Получить {} критических ударов за неделю', 'goal': (20, 50), 'reward_gold': 600, 'reward_exp': 300},
    {'name': 'Коллекционер', 'description': 'Добыть {} ресурсов за неделю', 'goal': (50, 150), 'reward_gold': 700, 'reward_exp': 350},
    {'name': 'Торговец', 'description': 'Продать ресурсов на {} золота за неделю', 'goal': (2000, 5000), 'reward_gold': 900, 'reward_exp': 450}
]

# -------------------- Стикеры (замените на свои) --------------------
STICKERS = {
    'crit': 'CAACAgIAAxkBAAEBuK1mM3Fhx7...',      # стикер для крита
    'achievement': 'CAACAgIAAxkBAAEBuK9mM3Gx8...', # стикер достижения
    'purchase': 'CAACAgIAAxkBAAEBuLFmM3Hx9...'     # стикер покупки
}

# -------------------- Ресурсы (базовые) --------------------
RESOURCES = {
    'coal': {'name': 'Уголь', 'base_price': 5},
    'iron': {'name': 'Железо', 'base_price': 10},
    'gold': {'name': 'Золотая руда', 'base_price': 30},
    'diamond': {'name': 'Алмаз', 'base_price': 100},
    'mithril': {'name': 'Мифрил', 'base_price': 300}
}

# -------------------- Локации (шахты) --------------------
LOCATIONS = {
    'coal_mine': {
        'name': 'Угольная шахта',
        'description': 'Мелкая шахта, где много угля. Идеально для новичков.',
        'min_level': 1,
        'min_tool_level': 0,
        'difficulty': 1.0,
        'resources': [
            {'res_id': 'coal', 'prob': 0.8, 'min': 1, 'max': 3},
            {'res_id': 'iron', 'prob': 0.2, 'min': 1, 'max': 1}
        ]
    },
    'iron_mine': {
        'name': 'Железный рудник',
        'description': 'Глубокая шахта с залежами железной руды.',
        'min_level': 3,
        'min_tool_level': 1,
        'difficulty': 1.2,
        'resources': [
            {'res_id': 'iron', 'prob': 0.7, 'min': 1, 'max': 2},
            {'res_id': 'coal', 'prob': 0.3, 'min': 1, 'max': 2},
            {'res_id': 'gold', 'prob': 0.1, 'min': 1, 'max': 1}
        ]
    },
    'gold_mine': {
        'name': 'Золотая жила',
        'description': 'Легендарное место, где золото встречается часто.',
        'min_level': 5,
        'min_tool_level': 2,
        'difficulty': 1.5,
        'resources': [
            {'res_id': 'gold', 'prob': 0.6, 'min': 1, 'max': 2},
            {'res_id': 'iron', 'prob': 0.3, 'min': 1, 'max': 2},
            {'res_id': 'diamond', 'prob': 0.1, 'min': 1, 'max': 1}
        ]
    },
    'diamond_cave': {
        'name': 'Алмазная пещера',
        'description': 'Редкие алмазы, но очень опасно.',
        'min_level': 10,
        'min_tool_level': 3,
        'difficulty': 2.0,
        'resources': [
            {'res_id': 'diamond', 'prob': 0.4, 'min': 1, 'max': 1},
            {'res_id': 'gold', 'prob': 0.4, 'min': 1, 'max': 2},
            {'res_id': 'mithril', 'prob': 0.2, 'min': 1, 'max': 1}
        ]
    },
    'mithril_mine': {
        'name': 'Мифриловые копи',
        'description': 'Древние копи, полные мифрила. Только для опытных.',
        'min_level': 20,
        'min_tool_level': 4,
        'difficulty': 2.5,
        'resources': [
            {'res_id': 'mithril', 'prob': 0.5, 'min': 1, 'max': 2},
            {'res_id': 'diamond', 'prob': 0.3, 'min': 1, 'max': 1},
            {'res_id': 'gold', 'prob': 0.2, 'min': 1, 'max': 3}
        ]
    }
}

# -------------------- Инструменты (кирки) --------------------
TOOLS = {
    'wooden_pickaxe': {
        'name': 'Деревянная кирка',
        'description': 'Самая простая кирка. Подходит только для угля.',
        'base_power': 1,
        'price': 0,
        'required_level': 1
    },
    'stone_pickaxe': {
        'name': 'Каменная кирка',
        'description': 'Немного прочнее. Можно добывать железо.',
        'base_power': 2,
        'price': 100,
        'required_level': 3
    },
    'iron_pickaxe': {
        'name': 'Железная кирка',
        'description': 'Хорошая кирка для большинства пород.',
        'base_power': 3,
        'price': 500,
        'required_level': 5
    },
    'golden_pickaxe': {
        'name': 'Золотая кирка',
        'description': 'Очень быстрая, но хрупкая. Увеличивает шанс крита.',
        'base_power': 2,
        'price': 1000,
        'required_level': 8
    },
    'diamond_pickaxe': {
        'name': 'Алмазная кирка',
        'description': 'Прочная и эффективная. Лучший выбор.',
        'base_power': 4,
        'price': 5000,
        'required_level': 15
    },
    'mithril_pickaxe': {
        'name': 'Мифриловая кирка',
        'description': 'Легендарный инструмент для добычи мифрила.',
        'base_power': 5,
        'price': 20000,
        'required_level': 25
    }
}

# ==================== КЛАСС ДОСТИЖЕНИЙ ====================
class Achievement:
    def __init__(self, id: str, name: str, description: str, condition_func, reward_gold: int = 0, reward_exp: int = 0):
        self.id = id
        self.name = name
        self.description = description
        self.condition_func = condition_func
        self.reward_gold = reward_gold
        self.reward_exp = reward_exp

# Условия достижений
def condition_first_click(user_id):
    stats = get_player_stats(user_id)
    return stats['clicks'] >= 1, stats['clicks'], 1

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

# ==================== ФУНКЦИИ РАБОТЫ С БД ====================
def init_db():
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
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
                  last_weekly_reset DATE,
                  current_location TEXT DEFAULT 'coal_mine')''')
    c.execute('''CREATE TABLE IF NOT EXISTS upgrades
                 (user_id INTEGER,
                  upgrade_id TEXT,
                  level INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, upgrade_id))''')
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
                  week TEXT,
                  PRIMARY KEY (user_id, task_id, week))''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_achievements
                 (user_id INTEGER,
                  achievement_id TEXT,
                  unlocked_at DATE,
                  progress INTEGER,
                  max_progress INTEGER,
                  PRIMARY KEY (user_id, achievement_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS inventory
                 (user_id INTEGER,
                  resource_id TEXT,
                  amount INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, resource_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS player_tools
                 (user_id INTEGER,
                  tool_id TEXT,
                  level INTEGER DEFAULT 1,
                  experience INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, tool_id))''')
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
        for upgrade_id in UPGRADES:
            c.execute('''INSERT INTO upgrades (user_id, upgrade_id, level) VALUES (?, ?, 0)''', (user_id, upgrade_id))
        for res_id in RESOURCES:
            c.execute('''INSERT INTO inventory (user_id, resource_id, amount) VALUES (?, ?, 0)''', (user_id, res_id))
        c.execute('''INSERT INTO player_tools (user_id, tool_id, level, experience) VALUES (?, ?, 1, 0)''', (user_id, 'wooden_pickaxe'))
        conn.commit()
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
    res = c.fetchone()
    conn.close()
    return res[0] if res else 0

def set_upgrade_level(user_id: int, upgrade_id: str, level: int):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE upgrades SET level = ? WHERE user_id = ? AND upgrade_id = ?", (level, user_id, upgrade_id))
    conn.commit()
    conn.close()

# -------------------- Задания --------------------
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
    res = c.fetchone()
    if res:
        last_reset = res[0]
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
    res = c.fetchone()
    if res:
        last_reset = res[0]
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

# -------------------- Инвентарь и ресурсы --------------------
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

# -------------------- Инструменты --------------------
def get_player_tools(user_id: int) -> Dict[str, int]:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT tool_id, level FROM player_tools WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return {tool_id: level for tool_id, level in rows}

def add_tool(user_id: int, tool_id: str):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO player_tools (user_id, tool_id, level, experience) VALUES (?, ?, 1, 0)",
              (user_id, tool_id))
    conn.commit()
    conn.close()

def has_tool(user_id: int, tool_id: str) -> bool:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM player_tools WHERE user_id = ? AND tool_id = ?", (user_id, tool_id))
    res = c.fetchone()
    conn.close()
    return res is not None

# -------------------- Локации --------------------
def get_player_current_location(user_id: int) -> str:
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT current_location FROM players WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res[0] if res else 'coal_mine'

def set_player_location(user_id: int, location_id: str):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET current_location = ? WHERE user_id = ?", (location_id, user_id))
    conn.commit()
    conn.close()

def get_available_locations(user_id: int):
    stats = get_player_stats(user_id)
    level = stats['level']
    available = []
    for loc_id, loc in LOCATIONS.items():
        if level >= loc['min_level']:
            available.append((loc_id, loc['name']))
    return available

# -------------------- Статистика --------------------
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

# -------------------- Анимации и достижения --------------------
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

# ==================== ВСПОМОГАТЕЛЬНЫЙ КЛАСС ДЛЯ КОМАНД ====================
class FakeQuery:
    """Имитирует callback_query для использования с функциями show_*"""
    def __init__(self, message, from_user):
        self.message = message
        self.from_user = from_user
        self.data = None
    async def answer(self, text=None, show_alert=False):
        if text:
            await self.message.reply_text(text)
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    get_player(user.id, user.username)
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображает главное меню с тремя основными кнопками."""
    keyboard = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine')],
        [InlineKeyboardButton("📋 Задания", callback_data='tasks')],
        [InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🪨 **Добро пожаловать в шахтёрскую глубину!**\n\n"
        "Твой путь к богатству начинается здесь.\n"
        "Используй команды из меню (кнопка слева внизу) или кнопки ниже.\n\n"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)

async def show_main_menu_from_query(query):
    """Отображает главное меню при возврате из разделов."""
    keyboard = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine')],
        [InlineKeyboardButton("📋 Задания", callback_data='tasks')],
        [InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🪨 **Главное меню**\n\n"
        "Используй команды из системного меню или кнопки ниже."
    )
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_main_menu_from_query: {e}")

# -------------------- Обработчики команд (для системного меню) --------------------
async def cmd_mine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await mine_action(fake, context)

async def cmd_locations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_locations(fake, context)

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_shop(fake, context)

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_tasks(fake, context)

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_profile(fake, context)

async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_inventory(fake, context)

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_market(fake, context)

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_player(user.id, user.username)
    fake = FakeQuery(update.message, update.effective_user)
    await show_leaderboard(fake, context)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🪨 **Шахтёрский бот**\n\n"
        "Ты начинающий шахтёр. Кликай, добывай ресурсы, продавай их, улучшай инструменты и открывай новые локации.\n\n"
        "**Команды:**\n"
        "/start - главное меню\n"
        "/mine - копнуть в текущей локации\n"
        "/locations - выбрать локацию\n"
        "/shop - магазин улучшений\n"
        "/tasks - задания\n"
        "/profile - твой профиль\n"
        "/inventory - ресурсы\n"
        "/market - продать ресурсы\n"
        "/leaderboard - топ игроков\n"
        "/help - это сообщение"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

# ==================== ОСНОВНЫЕ ДЕЙСТВИЯ ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    check_daily_reset(user_id)
    check_weekly_reset(user_id)

    if data == 'mine':
        await mine_action(query, context)
    elif data == 'locations':
        await show_locations(query, context)
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
    elif data.startswith('goto_'):
        await goto_location(query, context)
    elif data == 'back_to_menu':
        await show_main_menu_from_query(query)

async def mine_action(query, context):
    user_id = query.from_user.id
    location_id = get_player_current_location(user_id)
    location = LOCATIONS.get(location_id)
    if not location:
        location = LOCATIONS['coal_mine']
    
    rand = random.random()
    cumulative = 0
    found_resource = None
    amount = 0
    for res in location['resources']:
        cumulative += res['prob']
        if rand < cumulative:
            found_resource = res['res_id']
            amount = random.randint(res['min'], res['max'])
            break
    
    gold, exp, is_crit = get_click_reward(user_id)
    
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
    
    if found_resource:
        add_resource(user_id, found_resource, amount)
        resource_name = RESOURCES[found_resource]['name']
        resource_text = f"\nТы нашёл: {resource_name} x{amount}!"
    else:
        resource_text = ""
    
    update_daily_task_progress(user_id, 'Труженик', 1)
    update_daily_task_progress(user_id, 'Золотоискатель', gold)
    if is_crit:
        update_daily_task_progress(user_id, 'Везунчик', 1)
    if found_resource:
        update_daily_task_progress(user_id, 'Рудокоп', amount)
    
    update_weekly_task_progress(user_id, 'Шахтёр', 1)
    update_weekly_task_progress(user_id, 'Золотая лихорадка', gold)
    if is_crit:
        update_weekly_task_progress(user_id, 'Критический удар', 1)
    if found_resource:
        update_weekly_task_progress(user_id, 'Коллекционер', amount)
    
    if is_crit:
        await send_animation(context.bot, user_id, 'crit')
    await check_achievements(user_id, context)
    
    crit_text = "💥 КРИТ!" if is_crit else ""
    text = f"Ты добыл: {gold} золота {crit_text}{resource_text}\nПолучено опыта: {exp}"
    await query.message.reply_text(text)
    await show_main_menu_from_query(query)

# ==================== НОВАЯ ФУНКЦИЯ ПОКАЗА ЛОКАЦИЙ (с требованиями) ====================
async def show_locations(query, context):
    user_id = query.from_user.id
    current = get_player_current_location(user_id)
    stats = get_player_stats(user_id)
    level = stats['level']
    
    text = "🗺 **Локации:**\n\n"
    keyboard = []
    
    for loc_id, loc in LOCATIONS.items():
        # Проверяем доступность по уровню (можно добавить проверку инструментов позже)
        available = level >= loc['min_level']
        status = "✅" if available else "🔒"
        current_mark = "📍" if loc_id == current else ""
        
        line = f"{current_mark}{status} **{loc['name']}**"
        if not available:
            line += f" (требуется уровень {loc['min_level']})"
        else:
            line += f" (ур. {loc['min_level']}+)"
        text += line + "\n"
        text += f"   {loc['description']}\n\n"
        
        if available and loc_id != current:
            # Кнопка перехода только для доступных и не текущих
            keyboard.append([InlineKeyboardButton(f"Перейти в {loc['name']}", callback_data=f'goto_{loc_id}')])
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_locations: {e}")

async def goto_location(query, context):
    location_id = query.data.replace('goto_', '')
    user_id = query.from_user.id
    set_player_location(user_id, location_id)
    await query.answer(f"Ты переместился в {LOCATIONS[location_id]['name']}")
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
    text += "\n**Инструменты (кирки):**\n"
    for tool_id, tool in TOOLS.items():
        if tool['price'] > 0:
            if has_tool(user_id, tool_id):
                text += f"✅ {tool['name']} (уже есть)\n"
            else:
                text += f"{tool['name']} - {tool['price']}💰 (ур. {tool['required_level']})\n{tool['description']}\n\n"
                keyboard.append([InlineKeyboardButton(f"Купить {tool['name']} за {tool['price']}", callback_data=f'buy_tool_{tool_id}')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.error(f"Error in show_shop: {e}")

async def process_buy(query, context):
    data = query.data
    if data.startswith('buy_tool_'):
        tool_id = data.replace('buy_tool_', '')
        user_id = query.from_user.id
        tool = TOOLS.get(tool_id)
        if not tool:
            await query.answer("Ошибка: инструмент не найден", show_alert=True)
            return
        stats = get_player_stats(user_id)
        if stats['level'] < tool['required_level']:
            await query.answer(f"❌ Требуется уровень {tool['required_level']}", show_alert=True)
            return
        if stats['gold'] < tool['price']:
            await query.answer("❌ Недостаточно золота!", show_alert=True)
            return
        conn = sqlite3.connect('game.db')
        c = conn.cursor()
        c.execute("UPDATE players SET gold = gold - ? WHERE user_id = ?", (tool['price'], user_id))
        c.execute("INSERT OR IGNORE INTO player_tools (user_id, tool_id, level, experience) VALUES (?, ?, 1, 0)",
                  (user_id, tool_id))
        conn.commit()
        conn.close()
        await send_animation(context.bot, user_id, 'purchase', f"✅ Ты купил {tool['name']}!")
        await show_shop(query, context)
        return
    
    upgrade_id = data.replace('buy_', '')
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
    
    tools = get_player_tools(user_id)
    if tools:
        text += "\n🧰 Инструменты:\n"
        for tool_id, level in tools.items():
            tool = TOOLS.get(tool_id)
            if tool:
                text += f"• {tool['name']} (ур. {level})\n"
    
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
    parts = data.split('_')
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
    
    update_daily_task_progress(user_id, 'Продавец', total_gold)
    update_weekly_task_progress(user_id, 'Торговец', total_gold)
    
    await query.answer(f"✅ Продано {sell_qty} {RESOURCES[res_id]['name']} за {total_gold} золота!", show_alert=False)
    await show_market(query, context)

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER ====================
async def run_bot():
    logger.info("Starting bot polling...")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mine", cmd_mine))
    application.add_handler(CommandHandler("locations", cmd_locations))
    application.add_handler(CommandHandler("shop", cmd_shop))
    application.add_handler(CommandHandler("tasks", cmd_tasks))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("inventory", cmd_inventory))
    application.add_handler(CommandHandler("market", cmd_market))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(CommandHandler("help", cmd_help))
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
