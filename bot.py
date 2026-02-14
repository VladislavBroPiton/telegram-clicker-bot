import logging
import random
import sqlite3
import datetime
import asyncio
import os
from typing import Dict, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("No BOT_TOKEN environment variable set")

BASE_CLICK_REWARD = (5, 15)
BASE_EXP_REWARD = (1, 3)
EXP_PER_LEVEL = 100

UPGRADES = {
    'click_power': {'name': '⚡ Сила клика', 'description': '+2 золота за клик', 'base_price': 50, 'price_mult': 2.0, 'effect': {'click_gold': 2}},
    'crit_chance': {'name': '🍀 Шанс крита', 'description': '+2% шанс двойной добычи', 'base_price': 100, 'price_mult': 1.5, 'effect': {'crit_chance': 2}},
    'auto_clicker': {'name': '🤖 Автокликер', 'description': 'Доход каждые 10 мин', 'base_price': 200, 'price_mult': 2.0, 'effect': {'auto_income': 1}}
}

DAILY_TASK_TEMPLATES = [
    {'name': 'Труженик', 'description': 'Совершить {} кликов', 'goal': (10, 30), 'reward_gold': 50, 'reward_exp': 20},
    {'name': 'Золотоискатель', 'description': 'Заработать {} золота', 'goal': (100, 500), 'reward_gold': 100, 'reward_exp': 30},
    {'name': 'Покупатель', 'description': 'Купить улучшений на {} золота', 'goal': (150, 300), 'reward_gold': 80, 'reward_exp': 25},
    {'name': 'Везунчик', 'description': 'Получить {} критических ударов', 'goal': (3, 8), 'reward_gold': 70, 'reward_exp': 40},
    {'name': 'Рудокоп', 'description': 'Добыть {} ресурсов', 'goal': (5, 15), 'reward_gold': 60, 'reward_exp': 35},
    {'name': 'Продавец', 'description': 'Продать ресурсов на {} золота', 'goal': (200, 500), 'reward_gold': 90, 'reward_exp': 45}
]

WEEKLY_TASK_TEMPLATES = [
    {'name': 'Шахтёр-неделя', 'description': 'Совершить {} кликов', 'goal': (200, 500), 'reward_gold': 500, 'reward_exp': 200},
    {'name': 'Золотая лихорадка', 'description': 'Заработать {} золота', 'goal': (2000, 5000), 'reward_gold': 1000, 'reward_exp': 500},
    {'name': 'Магнат', 'description': 'Купить улучшений на {} золота', 'goal': (1500, 3000), 'reward_gold': 800, 'reward_exp': 400},
    {'name': 'Критический удар', 'description': 'Получить {} критических ударов', 'goal': (20, 50), 'reward_gold': 600, 'reward_exp': 300},
    {'name': 'Коллекционер', 'description': 'Добыть {} ресурсов', 'goal': (50, 150), 'reward_gold': 700, 'reward_exp': 350},
    {'name': 'Торговец', 'description': 'Продать ресурсов на {} золота', 'goal': (2000, 5000), 'reward_gold': 900, 'reward_exp': 450}
]

RESOURCES = {
    'coal': {'name': 'Уголь', 'base_price': 5},
    'iron': {'name': 'Железо', 'base_price': 10},
    'gold': {'name': 'Золотая руда', 'base_price': 30},
    'diamond': {'name': 'Алмаз', 'base_price': 100},
    'mithril': {'name': 'Мифрил', 'base_price': 300}
}

LOCATIONS = {
    'coal_mine': {'name': 'Угольная шахта', 'description': 'Мелкая шахта, много угля.', 'min_level': 1, 'resources': [{'res_id': 'coal', 'prob': 0.8, 'min': 1, 'max': 3}, {'res_id': 'iron', 'prob': 0.2, 'min': 1, 'max': 1}]},
    'iron_mine': {'name': 'Железный рудник', 'description': 'Залежи железной руды.', 'min_level': 3, 'resources': [{'res_id': 'iron', 'prob': 0.7, 'min': 1, 'max': 2}, {'res_id': 'coal', 'prob': 0.3, 'min': 1, 'max': 2}, {'res_id': 'gold', 'prob': 0.1, 'min': 1, 'max': 1}]},
    'gold_mine': {'name': 'Золотая жила', 'description': 'Богатое месторождение золота.', 'min_level': 5, 'resources': [{'res_id': 'gold', 'prob': 0.6, 'min': 1, 'max': 2}, {'res_id': 'iron', 'prob': 0.3, 'min': 1, 'max': 2}, {'res_id': 'diamond', 'prob': 0.1, 'min': 1, 'max': 1}]},
    'diamond_cave': {'name': 'Алмазная пещера', 'description': 'Редкие алмазы, опасно.', 'min_level': 10, 'resources': [{'res_id': 'diamond', 'prob': 0.4, 'min': 1, 'max': 1}, {'res_id': 'gold', 'prob': 0.4, 'min': 1, 'max': 2}, {'res_id': 'mithril', 'prob': 0.2, 'min': 1, 'max': 1}]},
    'mithril_mine': {'name': 'Мифриловые копи', 'description': 'Древние копи.', 'min_level': 20, 'resources': [{'res_id': 'mithril', 'prob': 0.5, 'min': 1, 'max': 2}, {'res_id': 'diamond', 'prob': 0.3, 'min': 1, 'max': 1}, {'res_id': 'gold', 'prob': 0.2, 'min': 1, 'max': 3}]}
}

TOOLS = {
    'wooden_pickaxe': {
        'name': 'Деревянная кирка',
        'description': 'Самая простая.',
        'price': 0,
        'required_level': 1,
        'base_power': 1,
        'upgrade_cost': {'coal': 5, 'iron': 2}
    },
    'stone_pickaxe': {
        'name': 'Каменная кирка',
        'description': 'Немного прочнее.',
        'price': 100,
        'required_level': 3,
        'base_power': 2,
        'upgrade_cost': {'coal': 10, 'iron': 5, 'gold': 1}
    },
    'iron_pickaxe': {
        'name': 'Железная кирка',
        'description': 'Хорошая кирка.',
        'price': 500,
        'required_level': 5,
        'base_power': 3,
        'upgrade_cost': {'coal': 20, 'iron': 10, 'gold': 3}
    },
    'golden_pickaxe': {
        'name': 'Золотая кирка',
        'description': 'Быстрая, но хрупкая.',
        'price': 1000,
        'required_level': 8,
        'base_power': 2,
        'upgrade_cost': {'coal': 30, 'iron': 15, 'gold': 10, 'diamond': 1}
    },
    'diamond_pickaxe': {
        'name': 'Алмазная кирка',
        'description': 'Прочная и эффективная.',
        'price': 5000,
        'required_level': 15,
        'base_power': 4,
        'upgrade_cost': {'coal': 50, 'iron': 30, 'gold': 20, 'diamond': 5}
    },
    'mithril_pickaxe': {
        'name': 'Мифриловая кирка',
        'description': 'Легендарная.',
        'price': 20000,
        'required_level': 25,
        'base_power': 5,
        'upgrade_cost': {'coal': 100, 'iron': 50, 'gold': 30, 'diamond': 10, 'mithril': 2}
    }
}

# ==================== ЧАСТО ЗАДАВАЕМЫЕ ВОПРОСЫ ====================
FAQ = [
    {
        "question": "🪨 Как добывать ресурсы?",
        "answer": "Нажимай кнопку «⛏ Добыть» в главном меню. Каждый клик приносит золото, опыт и случайные ресурсы в зависимости от текущей локации."
    },
    {
        "question": "🗺 Как открыть новые локации?",
        "answer": "Повышай уровень, кликая. Каждая новая локация требует определённый уровень. Список доступных локаций можно посмотреть по команде /locations. Там же отображается следующая локация и условия её открытия."
    },
    {
        "question": "🧰 Зачем нужны инструменты?",
        "answer": "Инструменты (кирки) увеличивают количество добываемых ресурсов. Их можно купить в магазине за золото, а затем улучшать за ресурсы. Чем выше уровень инструмента, тем больше ресурсов ты добываешь за клик."
    },
    {
        "question": "📋 Что такое ежедневные и еженедельные задания?",
        "answer": "Каждый день появляются 3 случайных задания, а каждую неделю – 2 более сложных. Выполняй их, чтобы получать дополнительное золото и опыт. Задания обновляются автоматически."
    },
    {
        "question": "💰 Как продать ресурсы?",
        "answer": "Зайди в раздел «💰 Рынок» (команда /market). Ты увидишь список своих ресурсов и текущие цены. Можно продать 1 единицу или всё количество сразу."
    },
    {
        "question": "🏆 Что такое достижения?",
        "answer": "Достижения – это особые цели, за выполнение которых даются награды (золото и опыт). Список полученных достижений можно увидеть в профиле (команда /profile)."
    },
    {
        "question": "⚡ Как увеличить доход за клик?",
        "answer": "Покупай улучшения в магазине (категория «⚡ Улучшения»). «Сила клика» прямо увеличивает золото за клик, а «Шанс крита» даёт шанс удвоить добычу."
    },
    {
        "question": "🔄 Как сменить активный инструмент?",
        "answer": "В магазине в категории «🧰 Инструменты» нажми кнопку «🔨 Сделать активным» рядом с нужным инструментом. Активный инструмент используется при добыче."
    }
]

class Achievement:
    def __init__(self, id, name, desc, cond, reward_gold=0, reward_exp=0):
        self.id, self.name, self.description, self.condition_func, self.reward_gold, self.reward_exp = id, name, desc, cond, reward_gold, reward_exp

def cond_first_click(uid): s=get_player_stats(uid); return s['clicks']>=1, s['clicks'], 1
def cond_clicks_100(uid): s=get_player_stats(uid); return s['clicks']>=100, s['clicks'], 100
def cond_gold_1000(uid): s=get_player_stats(uid); return s['total_gold']>=1000, s['total_gold'], 1000
def cond_crits_50(uid):
    conn=sqlite3.connect('game.db'); c=conn.cursor(); c.execute("SELECT total_crits FROM players WHERE user_id=?",(uid,)); r=c.fetchone()[0]; conn.close(); return r>=50, r, 50
def cond_crit_streak_5(uid):
    conn=sqlite3.connect('game.db'); c=conn.cursor(); c.execute("SELECT max_crit_streak FROM players WHERE user_id=?",(uid,)); r=c.fetchone()[0]; conn.close(); return r>=5, r, 5
def cond_resources_50(uid): inv=get_inventory(uid); total=sum(inv.values()); return total>=50, total, 50

ACHIEVEMENTS = [
    Achievement('first_click', 'Первый шаг', 'Сделать первый клик', cond_first_click, 10, 5),
    Achievement('clicks_100', 'Трудоголик', 'Сделать 100 кликов', cond_clicks_100, 50, 20),
    Achievement('gold_1000', 'Золотая жила', 'Добыть 1000 золота', cond_gold_1000, 100, 50),
    Achievement('crits_50', 'Критическая масса', 'Получить 50 критических ударов', cond_crits_50, 80, 30),
    Achievement('crit_streak_5', 'Везунчик', 'Серия критов 5', cond_crit_streak_5, 60, 25),
    Achievement('resources_50', 'Коллекционер', 'Собрать 50 ресурсов', cond_resources_50, 70, 35)
]

def init_db():
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    
    # Таблица players (с новым полем active_tool)
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
                  current_location TEXT DEFAULT 'coal_mine',
                  active_tool TEXT DEFAULT 'wooden_pickaxe')''')
    
    # Миграция: добавляем поле active_tool, если его нет (для старых баз)
    try:
        c.execute("ALTER TABLE players ADD COLUMN active_tool TEXT DEFAULT 'wooden_pickaxe'")
        logger.info("Column 'active_tool' added to players table.")
    except sqlite3.OperationalError:
        # Поле уже существует – игнорируем
        pass

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
                  week TEXT,
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

    # Таблица инструментов игрока
    c.execute('''CREATE TABLE IF NOT EXISTS player_tools
                 (user_id INTEGER,
                  tool_id TEXT,
                  level INTEGER DEFAULT 1,
                  experience INTEGER DEFAULT 0,
                  PRIMARY KEY (user_id, tool_id))''')

    conn.commit()
    conn.close()

def get_player(uid, username=None):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT * FROM players WHERE user_id=?", (uid,))
    p = c.fetchone()
    if not p:
        today = datetime.date.today().isoformat()
        cur_week = get_week_number()
        c.execute("INSERT INTO players (user_id, username, last_daily_reset, last_weekly_reset) VALUES (?,?,?,?)",
                  (uid, username, today, cur_week))
        for uid2 in UPGRADES:
            c.execute("INSERT INTO upgrades (user_id, upgrade_id, level) VALUES (?,?,0)", (uid, uid2))
        for rid in RESOURCES:
            c.execute("INSERT INTO inventory (user_id, resource_id, amount) VALUES (?,?,0)", (uid, rid))
        c.execute("INSERT INTO player_tools (user_id, tool_id, level, experience) VALUES (?,?,1,0)", (uid, 'wooden_pickaxe'))
        conn.commit()
        generate_daily_tasks(uid, conn)
        generate_weekly_tasks(uid, conn)
        conn.commit()
        c.execute("SELECT * FROM players WHERE user_id=?", (uid,))
        p = c.fetchone()
    conn.close()
    return p

def update_player(uid, **kwargs):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    set_clause = ', '.join([f"{k}=?" for k in kwargs])
    vals = list(kwargs.values()) + [uid]
    c.execute(f"UPDATE players SET {set_clause} WHERE user_id=?", vals)
    conn.commit()
    conn.close()

def get_upgrade_level(uid, uid2):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level FROM upgrades WHERE user_id=? AND upgrade_id=?", (uid, uid2))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 0

def set_upgrade_level(uid, uid2, lvl):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE upgrades SET level=? WHERE user_id=? AND upgrade_id=?", (lvl, uid, uid2))
    conn.commit()
    conn.close()

def generate_daily_tasks(uid, conn=None):
    close = False
    if conn is None:
        conn = sqlite3.connect('game.db')
        close = True
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute("DELETE FROM daily_tasks WHERE user_id=? AND date=?", (uid, today))
    templates = random.sample(DAILY_TASK_TEMPLATES, min(3, len(DAILY_TASK_TEMPLATES)))
    for i, t in enumerate(templates):
        goal = random.randint(*t['goal'])
        desc = t['description'].format(goal)
        c.execute("INSERT INTO daily_tasks (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, date) VALUES (?,?,?,?,?,?,?,?)",
                  (uid, i, t['name'], desc, goal, t['reward_gold'], t['reward_exp'], today))
    conn.commit()
    if close:
        conn.close()

def check_daily_reset(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT last_daily_reset FROM players WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r:
        last = r[0]
        today = datetime.date.today().isoformat()
        if last != today:
            generate_daily_tasks(uid, conn)
            c.execute("UPDATE players SET last_daily_reset=? WHERE user_id=?", (today, uid))
            conn.commit()
    conn.close()

def get_daily_tasks(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute("SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp FROM daily_tasks WHERE user_id=? AND date=?", (uid, today))
    tasks = c.fetchall()
    conn.close()
    return tasks

def update_daily_task_progress(uid, name_contains, delta):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    today = datetime.date.today().isoformat()
    c.execute("UPDATE daily_tasks SET progress=progress+? WHERE user_id=? AND date=? AND completed=0 AND task_name LIKE ?",
              (delta, uid, today, f'%{name_contains}%'))
    conn.commit()
    c.execute("SELECT task_id, goal, reward_gold, reward_exp FROM daily_tasks WHERE user_id=? AND date=? AND completed=0", (uid, today))
    tasks = c.fetchall()
    for tid, goal, rg, re in tasks:
        c.execute("SELECT progress FROM daily_tasks WHERE user_id=? AND task_id=? AND date=?", (uid, tid, today))
        prog = c.fetchone()[0]
        if prog >= goal:
            c.execute("UPDATE daily_tasks SET completed=1 WHERE user_id=? AND task_id=? AND date=?", (uid, tid, today))
            c.execute("UPDATE players SET gold=gold+?, exp=exp+? WHERE user_id=?", (rg, re, uid))
    conn.commit()
    conn.close()

def get_week_number(d=None):
    if d is None:
        d = datetime.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-{w:02d}"

def generate_weekly_tasks(uid, conn=None):
    close = False
    if conn is None:
        conn = sqlite3.connect('game.db')
        close = True
    c = conn.cursor()
    week = get_week_number()
    c.execute("DELETE FROM weekly_tasks WHERE user_id=? AND week=?", (uid, week))
    templates = random.sample(WEEKLY_TASK_TEMPLATES, min(2, len(WEEKLY_TASK_TEMPLATES)))
    for i, t in enumerate(templates):
        goal = random.randint(*t['goal'])
        desc = t['description'].format(goal)
        c.execute("INSERT INTO weekly_tasks (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, week) VALUES (?,?,?,?,?,?,?,?)",
                  (uid, i, t['name'], desc, goal, t['reward_gold'], t['reward_exp'], week))
    conn.commit()
    if close:
        conn.close()

def check_weekly_reset(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT last_weekly_reset FROM players WHERE user_id=?", (uid,))
    r = c.fetchone()
    if r:
        last = r[0]
        cur = get_week_number()
        if last != cur:
            generate_weekly_tasks(uid, conn)
            c.execute("UPDATE players SET last_weekly_reset=? WHERE user_id=?", (cur, uid))
            conn.commit()
    conn.close()

def get_weekly_tasks(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    week = get_week_number()
    c.execute("SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp FROM weekly_tasks WHERE user_id=? AND week=?", (uid, week))
    tasks = c.fetchall()
    conn.close()
    return tasks

def update_weekly_task_progress(uid, name_contains, delta):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    week = get_week_number()
    c.execute("UPDATE weekly_tasks SET progress=progress+? WHERE user_id=? AND week=? AND completed=0 AND task_name LIKE ?",
              (delta, uid, week, f'%{name_contains}%'))
    conn.commit()
    c.execute("SELECT task_id, goal, reward_gold, reward_exp FROM weekly_tasks WHERE user_id=? AND week=? AND completed=0", (uid, week))
    tasks = c.fetchall()
    for tid, goal, rg, re in tasks:
        c.execute("SELECT progress FROM weekly_tasks WHERE user_id=? AND task_id=? AND week=?", (uid, tid, week))
        prog = c.fetchone()[0]
        if prog >= goal:
            c.execute("UPDATE weekly_tasks SET completed=1 WHERE user_id=? AND task_id=? AND week=?", (uid, tid, week))
            c.execute("UPDATE players SET gold=gold+?, exp=exp+? WHERE user_id=?", (rg, re, uid))
    conn.commit()
    conn.close()

def get_inventory(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT resource_id, amount FROM inventory WHERE user_id=?", (uid,))
    rows = c.fetchall()
    conn.close()
    return {rid: amt for rid, amt in rows}

def add_resource(uid, rid, amt=1):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE inventory SET amount=amount+? WHERE user_id=? AND resource_id=?", (amt, uid, rid))
    conn.commit()
    conn.close()

def remove_resource(uid, rid, amt=1):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT amount FROM inventory WHERE user_id=? AND resource_id=?", (uid, rid))
    r = c.fetchone()
    if not r or r[0] < amt:
        conn.close()
        return False
    c.execute("UPDATE inventory SET amount=amount-? WHERE user_id=? AND resource_id=?", (amt, uid, rid))
    conn.commit()
    conn.close()
    return True

def get_player_tools(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT tool_id, level FROM player_tools WHERE user_id=?", (uid,))
    rows = c.fetchall()
    conn.close()
    return {tid: lvl for tid, lvl in rows}

def add_tool(uid, tid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO player_tools (user_id, tool_id, level, experience) VALUES (?,?,1,0)", (uid, tid))
    conn.commit()
    conn.close()

def has_tool(uid, tid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM player_tools WHERE user_id=? AND tool_id=?", (uid, tid))
    r = c.fetchone()
    conn.close()
    return r is not None

def get_tool_level(uid, tid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level FROM player_tools WHERE user_id=? AND tool_id=?", (uid, tid))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 0

def get_tool_power(uid, tid):
    level = get_tool_level(uid, tid)
    if level == 0:
        return 0
    base = TOOLS[tid]['base_power']
    return base + level - 1

def get_upgrade_cost(uid, tid):
    level = get_tool_level(uid, tid)
    if level == 0:
        return {}
    base_cost = TOOLS[tid]['upgrade_cost']
    return {res: amount * level for res, amount in base_cost.items()}

def can_upgrade_tool(uid, tid):
    level = get_tool_level(uid, tid)
    if level == 0:
        return False
    cost = get_upgrade_cost(uid, tid)
    inv = get_inventory(uid)
    for res, need in cost.items():
        if inv.get(res, 0) < need:
            return False
    return True

def upgrade_tool(uid, tid):
    if not can_upgrade_tool(uid, tid):
        return False
    cost = get_upgrade_cost(uid, tid)
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    for res, need in cost.items():
        c.execute("UPDATE inventory SET amount = amount - ? WHERE user_id = ? AND resource_id = ?", (need, uid, res))
    c.execute("UPDATE player_tools SET level = level + 1 WHERE user_id = ? AND tool_id = ?", (uid, tid))
    conn.commit()
    conn.close()
    return True

def get_active_tool(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT active_tool FROM players WHERE user_id=?", (uid,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 'wooden_pickaxe'

def set_active_tool(uid, tid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET active_tool=? WHERE user_id=?", (tid, uid))
    conn.commit()
    conn.close()

def get_player_current_location(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT current_location FROM players WHERE user_id=?", (uid,))
    r = c.fetchone()
    conn.close()
    return r[0] if r else 'coal_mine'

def set_player_location(uid, loc):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET current_location=? WHERE user_id=?", (loc, uid))
    conn.commit()
    conn.close()

def get_player_stats(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level, exp, gold, total_clicks, total_gold_earned, total_crits, current_crit_streak, max_crit_streak FROM players WHERE user_id=?", (uid,))
    r = c.fetchone()
    if not r:
        conn.close()
        return {}
    lvl, exp, gold, clicks, tg, crits, cstreak, mstreak = r
    ups = {}
    for uid2 in UPGRADES:
        c.execute("SELECT level FROM upgrades WHERE user_id=? AND upgrade_id=?", (uid, uid2))
        res = c.fetchone()
        ups[uid2] = res[0] if res else 0
    conn.close()
    return {
        'level': lvl,
        'exp': exp,
        'exp_next': EXP_PER_LEVEL,
        'gold': gold,
        'clicks': clicks,
        'total_gold': tg,
        'total_crits': crits,
        'current_crit_streak': cstreak,
        'max_crit_streak': mstreak,
        'upgrades': ups
    }

def get_click_reward(uid):
    s = get_player_stats(uid)
    cpl = s['upgrades']['click_power']
    ccl = s['upgrades']['crit_chance']
    bg = random.randint(*BASE_CLICK_REWARD)
    be = random.randint(*BASE_EXP_REWARD)
    gold = bg + cpl * 2
    crit = (ccl * 2) / 100.0
    is_crit = random.random() < crit
    if is_crit:
        gold *= 2
        be *= 2
    return gold, be, is_crit

def level_up_if_needed(uid):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT level, exp FROM players WHERE user_id=?", (uid,))
    lvl, exp = c.fetchone()
    while exp >= EXP_PER_LEVEL:
        lvl += 1
        exp -= EXP_PER_LEVEL
    c.execute("UPDATE players SET level=?, exp=? WHERE user_id=?", (lvl, exp, uid))
    conn.commit()
    conn.close()
    async def check_achievements(uid, ctx):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT achievement_id FROM user_achievements WHERE user_id=?", (uid,))
    unlocked = {r[0] for r in c.fetchall()}
    new_ach = []
    for ach in ACHIEVEMENTS:
        if ach.id in unlocked:
            continue
        achieved, prog, maxp = ach.condition_func(uid)
        if achieved:
            today = datetime.date.today().isoformat()
            c.execute("INSERT INTO user_achievements (user_id, achievement_id, unlocked_at, progress, max_progress) VALUES (?,?,?,?,?)",
                      (uid, ach.id, today, prog, maxp))
            c.execute("UPDATE players SET gold=gold+?, exp=exp+? WHERE user_id=?", (ach.reward_gold, ach.reward_exp, uid))
            new_ach.append(ach)
    conn.commit()
    conn.close()
    for ach in new_ach:
        txt = f"🏆 Достижение получено: {ach.name}\n{ach.description}"
        if ach.reward_gold > 0 or ach.reward_exp > 0:
            txt += f"\nНаграда: {ach.reward_gold}💰, {ach.reward_exp}✨"
        await ctx.bot.send_message(chat_id=uid, text=txt)
    return len(new_ach)

class FakeQuery:
    def __init__(self, msg, from_user):
        self.message = msg
        self.from_user = from_user
        self.data = None
    async def answer(self, text=None, show_alert=False):
        if text:
            await self.message.reply_text(text)
    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        await self.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

async def start(update: Update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_main_menu(update, ctx)

async def show_main_menu(update: Update, ctx):
    kb = [[
        InlineKeyboardButton("⛏ Добыть", callback_data='mine'),
        InlineKeyboardButton("📋 Задания", callback_data='tasks'),
        InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard_menu')
    ]]
    rm = InlineKeyboardMarkup(kb)
    txt = ("🪨 **Шахтёрская глубина**\n\n"
           "Привет, шахтёр! Твой путь к богатству начинается здесь.\n\n"
           "🏁 **Что делать?**\n"
           "• Нажимай «⛏ Добыть» – каждый клик приносит золото и ресурсы.\n"
           "• Выполняй «📋 Задания» – получай бонусы.\n"
           "• Соревнуйся в «🏆 Лидеры» – стань лучшим!\n\n"
           "Остальные команды доступны в меню (кнопка слева внизу).")
    if update.callback_query:
        await update.callback_query.edit_message_text(txt, parse_mode='Markdown', reply_markup=rm)
    else:
        await update.message.reply_text(txt, parse_mode='Markdown', reply_markup=rm)

async def show_main_menu_from_query(query):
    kb = [[
        InlineKeyboardButton("⛏ Добыть", callback_data='mine'),
        InlineKeyboardButton("📋 Задания", callback_data='tasks'),
        InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard_menu')
    ]]
    rm = InlineKeyboardMarkup(kb)
    txt = ("🪨 **Главное меню**\n\n"
           "🏁 **Куда идём?**\n"
           "• ⛏ Добыча – вперёд за ресурсами!\n"
           "• 📋 Задания – ежедневные и еженедельные.\n"
           "• 🏆 Лидеры – посмотреть топ игроков.\n\n"
           "Остальные команды – в меню Telegram.")
    try:
        await query.edit_message_text(txt, parse_mode='Markdown', reply_markup=rm)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def cmd_mine(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await mine_action(FakeQuery(update.message, u), ctx)

async def cmd_locations(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_locations(FakeQuery(update.message, u), ctx)

async def cmd_shop(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_shop_menu(FakeQuery(update.message, u), ctx)

async def cmd_tasks(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_tasks(FakeQuery(update.message, u), ctx)

async def cmd_profile(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_profile(FakeQuery(update.message, u), ctx)

async def cmd_inventory(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_inventory(FakeQuery(update.message, u), ctx)

async def cmd_market(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_market(FakeQuery(update.message, u), ctx)

async def cmd_leaderboard(update, ctx):
    u = update.effective_user
    get_player(u.id, u.username)
    await show_leaderboard_menu(FakeQuery(update.message, u), ctx)

async def cmd_faq(update, ctx):
    """Отправляет красиво оформленный список часто задаваемых вопросов и ответов."""
    # Словарь вопрос->ответ (можно оставить как есть)
    faq_dict = {item["question"]: item["answer"] for item in FAQ}
    
    # Группируем вопросы по категориям
    categories = {
        "🪨 **Основное**": [
            "🪨 Как добывать ресурсы?",
            "🧰 Зачем нужны инструменты?",
            "⚡ Как увеличить доход за клик?"
        ],
        "🗺 **Локации**": [
            "🗺 Как открыть новые локации?"
        ],
        "📋 **Задания**": [
            "📋 Что такое ежедневные и еженедельные задания?"
        ],
        "💰 **Экономика**": [
            "💰 Как продать ресурсы?",
            "🏆 Что такое достижения?"
        ],
        "🔄 **Инструменты**": [
            "🔄 Как сменить активный инструмент?"
        ]
    }
    
    text = "📚 **Часто задаваемые вопросы**\n\n"
    
    for category, questions in categories.items():
        text += f"{category}\n" + "─" * 25 + "\n\n"
        for q in questions:
            if q in faq_dict:
                text += f"❓ **{q}**\n{faq_dict[q]}\n\n"
        text += "\n"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_help(update, ctx):
    txt = ("🪨 **Шахтёрский бот**\n\n"
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
           "/faq - часто задаваемые вопросы\n"
           "/help - это сообщение")
    await update.message.reply_text(txt, parse_mode='Markdown')

async def button_handler(update: Update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    check_daily_reset(uid)
    check_weekly_reset(uid)
    if data == 'mine':
        await mine_action(q, ctx)
    elif data == 'locations':
        await show_locations(q, ctx)
    elif data == 'shop':
        await show_shop_menu(q, ctx)
    elif data == 'shop_category_upgrades':
        await show_shop_upgrades(q, ctx)
    elif data == 'shop_category_tools':
        await show_shop_tools(q, ctx)
    elif data == 'back_to_shop_menu':
        await show_shop_menu(q, ctx)
    elif data == 'back_to_shop_tools':
        await show_shop_tools(q, ctx)
    elif data.startswith('activate_tool_'):
        await activate_tool(q, ctx)
    elif data.startswith('upgrade_tool_'):
        await upgrade_tool_handler(q, ctx)
    elif data.startswith('confirm_upgrade_'):
        await confirm_upgrade(q, ctx)
    elif data == 'tasks':
        await show_tasks(q, ctx)
    elif data == 'profile':
        await show_profile(q, ctx)
    elif data == 'leaderboard_menu':
        await show_leaderboard_menu(q, ctx)
    elif data == 'leaderboard_level':
        await show_leaderboard_level(q, ctx)
    elif data == 'leaderboard_gold':
        await show_leaderboard_gold(q, ctx)
    elif data == 'leaderboard_coal':
        await show_leaderboard_coal(q, ctx)
    elif data == 'leaderboard_iron':
        await show_leaderboard_iron(q, ctx)
    elif data == 'leaderboard_gold_ore':
        await show_leaderboard_gold_ore(q, ctx)
    elif data == 'leaderboard_diamond':
        await show_leaderboard_diamond(q, ctx)
    elif data == 'leaderboard_mithril':
        await show_leaderboard_mithril(q, ctx)
    elif data == 'inventory':
        await show_inventory(q, ctx)
    elif data == 'market':
        await show_market(q, ctx)
    elif data.startswith('buy_'):
        await process_buy(q, ctx)
    elif data.startswith('sell_'):
        await process_sell(q, ctx)
    elif data.startswith('goto_'):
        await goto_location(q, ctx)
    elif data == 'back_to_menu':
        await show_main_menu_from_query(q)

async def mine_action(q, ctx):
    uid = q.from_user.id
    loc = get_player_current_location(uid)
    loc = LOCATIONS.get(loc, LOCATIONS['coal_mine'])
    rnd = random.random()
    cum = 0
    found = None
    amt = 0
    for r in loc['resources']:
        cum += r['prob']
        if rnd < cum:
            found = r['res_id']
            amt = random.randint(r['min'], r['max'])
            break
    gold, exp, is_crit = get_click_reward(uid)
    if found:
        active_tool = get_active_tool(uid)
        tool_power = get_tool_power(uid, active_tool)
        if tool_power > 0:
            multiplier = 1 + (tool_power - 1) * 0.2
            amt = int(amt * multiplier)
            amt = max(1, amt)
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET gold=gold+?, exp=exp+?, total_clicks=total_clicks+1, total_gold_earned=total_gold_earned+? WHERE user_id=?",
              (gold, exp, gold, uid))
    if is_crit:
        c.execute("UPDATE players SET total_crits=total_crits+1, current_crit_streak=current_crit_streak+1, max_crit_streak=MAX(max_crit_streak, current_crit_streak) WHERE user_id=?", (uid,))
    else:
        c.execute("UPDATE players SET current_crit_streak=0 WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    level_up_if_needed(uid)
    if found:
        add_resource(uid, found, amt)
        res_txt = f"\nТы нашёл: {RESOURCES[found]['name']} x{amt}!"
    else:
        res_txt = ""
    update_daily_task_progress(uid, 'Труженик', 1)
    update_daily_task_progress(uid, 'Золотоискатель', gold)
    if is_crit:
        update_daily_task_progress(uid, 'Везунчик', 1)
    if found:
        update_daily_task_progress(uid, 'Рудокоп', amt)
    update_weekly_task_progress(uid, 'Шахтёр', 1)
    update_weekly_task_progress(uid, 'Золотая лихорадка', gold)
    if is_crit:
        update_weekly_task_progress(uid, 'Критический удар', 1)
    if found:
        update_weekly_task_progress(uid, 'Коллекционер', amt)
    await check_achievements(uid, ctx)
    ct = "💥 КРИТ!" if is_crit else ""
    txt = f"Ты добыл: {gold} золота {ct}{res_txt}\nПолучено опыта: {exp}"
    await q.message.reply_text(txt)
    await show_main_menu_from_query(q)

async def show_locations(q, ctx):
    uid = q.from_user.id
    cur = get_player_current_location(uid)
    stats = get_player_stats(uid)
    lvl = stats['level']
    sl = sorted(LOCATIONS.items(), key=lambda x: x[1]['min_level'])
    cur_idx = None
    for i, (lid, _) in enumerate(sl):
        if lid == cur:
            cur_idx = i
            break
    if cur_idx is None:
        cur_idx = 0
    idxs = [cur_idx]
    if cur_idx + 1 < len(sl):
        idxs.append(cur_idx + 1)
    txt = "🗺 **Локации**\n\n"
    kb = []
    for i in idxs:
        lid, loc = sl[i]
        avail = lvl >= loc['min_level']
        is_cur = (lid == cur)
        status = "✅" if avail else "🔒"
        mark = "📍" if is_cur else ""
        line = f"{mark}{status} **{loc['name']}**"
        if not avail:
            line += f" (требуется ур.{loc['min_level']})"
        else:
            line += f" (доступна, ур.{loc['min_level']}+)"
        txt += line + "\n   " + loc['description'] + "\n\n"
        if avail and not is_cur:
            kb.append([InlineKeyboardButton(f"Перейти в {loc['name']}", callback_data=f'goto_{lid}')])
    txt += "─────────────────────────\nХочешь сменить локацию? Нажми на кнопку ниже (если она доступна)."
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def goto_location(q, ctx):
    lid = q.data.replace('goto_', '')
    uid = q.from_user.id
    set_player_location(uid, lid)
    await q.answer(f"Ты переместился в {LOCATIONS[lid]['name']}")
    await show_main_menu_from_query(q)

async def show_shop_menu(q, ctx):
    kb = [
        [InlineKeyboardButton("⚡ Улучшения", callback_data='shop_category_upgrades')],
        [InlineKeyboardButton("🧰 Инструменты", callback_data='shop_category_tools')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    txt = ("🛒 **Магазин**\n\n"
           "Здесь ты можешь улучшить своего шахтёра. Выбери категорию:\n\n"
           "⚡ Улучшения – прокачка навыков\n"
           "🧰 Инструменты – покупка и улучшение кирок")
    try:
        await q.edit_message_text(txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_shop_upgrades(q, ctx):
    uid = q.from_user.id
    stats = get_player_stats(uid)
    gold = stats['gold']
    txt = f"⚡ **Улучшения**\n💰 Твой баланс: {gold} золота\n\n"
    kb = []
    for uid2, info in UPGRADES.items():
        lvl = stats['upgrades'][uid2]
        price = int(info['base_price'] * (info['price_mult'] ** lvl))
        txt += f"─────────────────────────\n**{info['name']}** (ур.{lvl})\n   {info['description']}\n   💰 Следующий уровень: {price}\n\n"
        kb.append([InlineKeyboardButton(f"Купить {info['name']} за {price}", callback_data=f'buy_{uid2}')])
    txt += "─────────────────────────\nЧтобы купить, нажми на кнопку ниже."
    kb.append([InlineKeyboardButton("🔙 В меню магазина", callback_data='back_to_shop_menu')])
    try:
        await q.edit_message_text(txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_shop_tools(q, ctx):
    uid = q.from_user.id
    stats = get_player_stats(uid)
    gold = stats['gold']
    active = get_active_tool(uid)
    txt = f"🧰 **Инструменты**\n💰 Твой баланс: {gold} золота\n\n"
    kb = []
    for tid, tool in TOOLS.items():
        level = get_tool_level(uid, tid)
        if level == 0 and tool['price'] > 0:
            txt += f"─────────────────────────\n🔒 **{tool['name']}** – {tool['price']}💰 (треб.ур.{tool['required_level']})\n   {tool['description']}\n\n"
            kb.append([InlineKeyboardButton(f"Купить {tool['name']} за {tool['price']}", callback_data=f'buy_tool_{tid}')])
        elif level > 0:
            is_active = (tid == active)
            active_mark = "📍" if is_active else ""
            power = get_tool_power(uid, tid)
            txt += f"─────────────────────────\n{active_mark} **{tool['name']}** ур.{level} (сила {power})\n   {tool['description']}\n"
            row = []
            if not is_active:
                row.append(InlineKeyboardButton("🔨 Сделать активным", callback_data=f'activate_tool_{tid}'))
            if can_upgrade_tool(uid, tid):
                cost = get_upgrade_cost(uid, tid)
                cost_str = ", ".join([f"{RESOURCES[res]['name']} {amt}" for res, amt in cost.items()])
                row.append(InlineKeyboardButton(f"⬆️ Улучшить ({cost_str})", callback_data=f'upgrade_tool_{tid}'))
            if row:
                kb.append(row)
    txt += "─────────────────────────\nВыбери действие."
    kb.append([InlineKeyboardButton("🔙 В меню магазина", callback_data='back_to_shop_menu')])
    try:
        await q.edit_message_text(txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def process_buy(q, ctx):
    data = q.data
    if data.startswith('buy_tool_'):
        tid = data.replace('buy_tool_', '')
        uid = q.from_user.id
        tool = TOOLS.get(tid)
        if not tool:
            await q.answer("Ошибка!", show_alert=True)
            return
        stats = get_player_stats(uid)
        if stats['level'] < tool['required_level']:
            await q.answer(f"❌ Требуется уровень {tool['required_level']}", show_alert=True)
            return
        if stats['gold'] < tool['price']:
            await q.answer("❌ Недостаточно золота!", show_alert=True)
            return
        conn = sqlite3.connect('game.db')
        c = conn.cursor()
        c.execute("UPDATE players SET gold=gold-? WHERE user_id=?", (tool['price'], uid))
        c.execute("INSERT OR IGNORE INTO player_tools (user_id, tool_id, level, experience) VALUES (?,?,1,0)", (uid, tid))
        conn.commit()
        conn.close()
        await ctx.bot.send_message(chat_id=uid, text=f"✅ Ты купил {tool['name']}!")
        await show_shop_tools(q, ctx)
        return
    uid2 = data.replace('buy_', '')
    uid = q.from_user.id
    stats = get_player_stats(uid)
    lvl = stats['upgrades'][uid2]
    price = int(UPGRADES[uid2]['base_price'] * (UPGRADES[uid2]['price_mult'] ** lvl))
    if stats['gold'] < price:
        await q.edit_message_text("❌ Недостаточно золота!")
        return
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("UPDATE players SET gold=gold-? WHERE user_id=?", (price, uid))
    c.execute("UPDATE upgrades SET level=level+1 WHERE user_id=? AND upgrade_id=?", (uid, uid2))
    conn.commit()
    conn.close()
    update_daily_task_progress(uid, 'Покупатель', price)
    update_weekly_task_progress(uid, 'Магнат', price)
    await ctx.bot.send_message(chat_id=uid, text=f"✅ {UPGRADES[uid2]['name']} улучшен до {lvl+1} уровня.")
    await check_achievements(uid, ctx)
    await show_shop_upgrades(q, ctx)

async def activate_tool(q, ctx):
    tid = q.data.replace('activate_tool_', '')
    uid = q.from_user.id
    set_active_tool(uid, tid)
    await q.answer(f"✅ {TOOLS[tid]['name']} теперь активна!")
    await show_shop_tools(q, ctx)

async def upgrade_tool_handler(q, ctx):
    tid = q.data.replace('upgrade_tool_', '')
    uid = q.from_user.id
    if not can_upgrade_tool(uid, tid):
        await q.answer("❌ Недостаточно ресурсов!", show_alert=True)
        await show_shop_tools(q, ctx)
        return
    cost = get_upgrade_cost(uid, tid)
    cost_text = "\n".join([f"{RESOURCES[res]['name']}: {amt}" for res, amt in cost.items()])
    kb = [[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f'confirm_upgrade_{tid}'),
        InlineKeyboardButton("❌ Отмена", callback_data='back_to_shop_tools')
    ]]
    await q.edit_message_text(
        f"⬆️ Улучшение {TOOLS[tid]['name']} до ур.{get_tool_level(uid, tid)+1}\n\nПотребуется:\n{cost_text}\n\nПодтверждаешь?",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def confirm_upgrade(q, ctx):
    tid = q.data.replace('confirm_upgrade_', '')
    uid = q.from_user.id
    if not can_upgrade_tool(uid, tid):
        await q.answer("❌ Недостаточно ресурсов!", show_alert=True)
        await show_shop_tools(q, ctx)
        return
    if upgrade_tool(uid, tid):
        new_level = get_tool_level(uid, tid)
        await q.answer("✅ Уровень повышен!")
        await ctx.bot.send_message(chat_id=uid, text=f"🔨 {TOOLS[tid]['name']} улучшена до уровня {new_level}!")
    else:
        await q.answer("❌ Ошибка при улучшении", show_alert=True)
    await show_shop_tools(q, ctx)

async def show_tasks(q, ctx):
    uid = q.from_user.id
    daily = get_daily_tasks(uid)
    weekly = get_weekly_tasks(uid)
    txt = "📋 **Ежедневные задания**\n\n"
    if daily:
        for t in daily:
            _, n, desc, g, prog, com, rg, re = t
            st = "✅" if com else f"{prog}/{g}"
            txt += f"🔹 **{n}**\n   {desc}\n   Прогресс: {st}\n   Награда: {rg}💰, {re}✨\n\n"
    else:
        txt += "Нет заданий на сегодня.\n\n"
    txt += "─────────────────────────\n📅 **Еженедельные задания**\n\n"
    if weekly:
        for t in weekly:
            _, n, desc, g, prog, com, rg, re = t
            st = "✅" if com else f"{prog}/{g}"
            txt += f"🔸 **{n}**\n   {desc}\n   Прогресс: {st}\n   Награда: {rg}💰, {re}✨\n\n"
    else:
        txt += "Нет заданий на эту неделю.\n\n"
    kb = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_profile(q, ctx):
    uid = q.from_user.id
    stats = get_player_stats(uid)
    if not stats:
        await q.edit_message_text("Профиль не найден.")
        return
    txt = (f"👤 **Профиль игрока**\n\n"
           f"📊 **Статистика**\n"
           f"• Уровень: **{stats['level']}**\n"
           f"• Опыт: **{stats['exp']}** / {stats['exp_next']}\n"
           f"• Золото: **{stats['gold']}**💰\n"
           f"• Всего кликов: **{stats['clicks']}**\n"
           f"• Всего добыто золота: **{stats['total_gold']}**💰\n"
           f"• Критические удары: **{stats['total_crits']}**\n"
           f"• Макс. серия критов: **{stats['max_crit_streak']}**\n\n"
           f"⚡ **Улучшения**\n"
           f"• Сила клика: ур.**{stats['upgrades']['click_power']}**\n"
           f"• Шанс крита: ур.**{stats['upgrades']['crit_chance']}**\n"
           f"• Автокликер: ур.**{stats['upgrades']['auto_clicker']}**\n")
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT achievement_id, unlocked_at FROM user_achievements WHERE user_id=? ORDER BY unlocked_at DESC LIMIT 5", (uid,))
    recent = c.fetchall()
    conn.close()
    if recent:
        txt += f"\n🏅 **Последние достижения**\n"
        for aid, dt in recent:
            ach = next((a for a in ACHIEVEMENTS if a.id == aid), None)
            if ach:
                txt += f"• {ach.name} ({dt})\n"
    else:
        txt += "\n🏅 **Последние достижения**\n• Пока нет\n"
    tools = get_player_tools(uid)
    if tools:
        txt += f"\n🧰 **Инструменты**\n"
        for tid, lvl in tools.items():
            tool = TOOLS.get(tid)
            if tool:
                txt += f"• {tool['name']} ур.{lvl}\n"
    kb = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_leaderboard_menu(q, ctx):
    kb = [
        [InlineKeyboardButton("📊 По уровню", callback_data='leaderboard_level')],
        [InlineKeyboardButton("💰 По золоту", callback_data='leaderboard_gold')],
        [InlineKeyboardButton("🪨 По углю", callback_data='leaderboard_coal')],
        [InlineKeyboardButton("⚙️ По железу", callback_data='leaderboard_iron')],
        [InlineKeyboardButton("🟡 По золотой руде", callback_data='leaderboard_gold_ore')],
        [InlineKeyboardButton("💎 По алмазам", callback_data='leaderboard_diamond')],
        [InlineKeyboardButton("🔮 По мифрилу", callback_data='leaderboard_mithril')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    txt = "🏆 **Таблица лидеров**\n\nВыбери, по какому показателю показать топ-10 игроков:"
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_leaderboard_level(q, ctx):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT username, level, exp FROM players ORDER BY level DESC, exp DESC LIMIT 10")
    top = c.fetchall()
    conn.close()
    txt = "📊 **Топ по уровню**\n\n"
    if not top:
        txt += "Пока нет данных."
    else:
        for i, (name, lvl, exp) in enumerate(top, 1):
            txt += f"{i}. {name or 'Аноним'} — уровень {lvl} (опыт {exp})\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_leaderboard_gold(q, ctx):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT username, gold FROM players ORDER BY gold DESC LIMIT 10")
    top = c.fetchall()
    conn.close()
    txt = "💰 **Топ по золоту**\n\n"
    if not top:
        txt += "Пока нет данных."
    else:
        for i, (name, gold) in enumerate(top, 1):
            txt += f"{i}. {name or 'Аноним'} — {gold}💰\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_leaderboard_resource(q, ctx, rid, rname):
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT p.username, i.amount FROM inventory i JOIN players p ON i.user_id=p.user_id WHERE i.resource_id=? ORDER BY i.amount DESC LIMIT 10", (rid,))
    top = c.fetchall()
    conn.close()
    txt = f"🏆 **Топ по {rname}**\n\n"
    if not top:
        txt += "Пока нет данных."
    else:
        for i, (name, amt) in enumerate(top, 1):
            txt += f"{i}. {name or 'Аноним'} — {amt} шт.\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_leaderboard_coal(q, ctx): await show_leaderboard_resource(q, ctx, 'coal', 'Уголь')
async def show_leaderboard_iron(q, ctx): await show_leaderboard_resource(q, ctx, 'iron', 'Железо')
async def show_leaderboard_gold_ore(q, ctx): await show_leaderboard_resource(q, ctx, 'gold', 'Золотая руда')
async def show_leaderboard_diamond(q, ctx): await show_leaderboard_resource(q, ctx, 'diamond', 'Алмазы')
async def show_leaderboard_mithril(q, ctx): await show_leaderboard_resource(q, ctx, 'mithril', 'Мифрил')

async def show_inventory(q, ctx):
    uid = q.from_user.id
    inv = get_inventory(uid)
    txt = "🎒 **Инвентарь**\n\nВот что ты накопал:\n\n"
    has = False
    for rid, info in RESOURCES.items():
        amt = inv.get(rid, 0)
        emoji = "🪨" if rid == 'coal' else "⚙️" if rid == 'iron' else "🟡" if rid == 'gold' else "💎" if rid == 'diamond' else "🔮"
        txt += f"{emoji} {info['name']}: **{amt}** шт.\n"
        if amt > 0: has = True
    if not has:
        txt = "🎒 **Инвентарь**\n\nТвой инвентарь пока пуст. Иди добывай!\n\n"
    txt += "\n─────────────────────────\nПродать ресурсы можно на рынке (/market)."
    kb = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def show_market(q, ctx):
    uid = q.from_user.id
    inv = get_inventory(uid)
    txt = "💰 **Рынок ресурсов**\n\nТвои запасы и текущие цены:\n\n"
    kb = []
    for rid, info in RESOURCES.items():
        amt = inv.get(rid, 0)
        price = info['base_price']
        emoji = "🪨" if rid == 'coal' else "⚙️" if rid == 'iron' else "🟡" if rid == 'gold' else "💎" if rid == 'diamond' else "🔮"
        txt += f"{emoji} {info['name']}: **{amt}** шт. | 💰 Цена: {price} за шт.\n"
        if amt > 0:
            kb.append([InlineKeyboardButton(f"Продать 1 {info['name']}", callback_data=f'sell_{rid}_1'),
                       InlineKeyboardButton(f"Продать всё", callback_data=f'sell_{rid}_all')])
    txt += "\n─────────────────────────\nВыбери, что и сколько продать."
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    try:
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error: {e}")

async def process_sell(q, ctx):
    data = q.data
    parts = data.split('_')
    rid = parts[1]
    sell_type = parts[2]
    uid = q.from_user.id
    conn = sqlite3.connect('game.db')
    c = conn.cursor()
    c.execute("SELECT amount FROM inventory WHERE user_id=? AND resource_id=?", (uid, rid))
    r = c.fetchone()
    if not r or r[0] == 0:
        await q.answer("Нет ресурса!", show_alert=True)
        conn.close()
        return
    avail = r[0]
    qty = avail if sell_type == 'all' else 1
    price = RESOURCES[rid]['base_price']
    total = qty * price
    c.execute("UPDATE inventory SET amount=amount-? WHERE user_id=? AND resource_id=?", (qty, uid, rid))
    c.execute("UPDATE players SET gold=gold+? WHERE user_id=?", (total, uid))
    conn.commit()
    conn.close()
    update_daily_task_progress(uid, 'Продавец', total)
    update_weekly_task_progress(uid, 'Торговец', total)
    await q.answer(f"✅ Продано {qty} {RESOURCES[rid]['name']} за {total}💰", show_alert=False)
    await show_market(q, ctx)

async def run_bot():
    logger.info("Starting bot polling...")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mine", cmd_mine))
    app.add_handler(CommandHandler("locations", cmd_locations))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("inventory", cmd_inventory))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("faq", cmd_faq))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(button_handler))
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("Bot polling started successfully")
        while True:
            await asyncio.sleep(10)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)

async def healthcheck(request):
    return JSONResponse({"status": "alive"})

async def startup_event():
    logger.info("Starting up...")
    init_db()
    asyncio.create_task(run_bot())

async def shutdown_event():
    logger.info("Shutting down...")

app = Starlette(routes=[Route("/healthcheck", healthcheck), Route("/", healthcheck)], on_startup=[startup_event], on_shutdown=[shutdown_event])

def main():
    init_db()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()
