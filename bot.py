"""
Telegram кликер бот "Шахтёрская глубина"
Финальная версия с улучшенной безопасностью, транзакциями и защитой от гонок.
Добавлены: крафт, выбор локаций, автосброс боссов, общий опыт в профиле.
"""

import logging
import random
import datetime
import asyncio
import os
import hashlib
import hmac
import json
from typing import Dict, Tuple, Optional, Any, List
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest
from telegram.helpers import escape_markdown
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
import uvicorn
import asyncpg
import time
from collections import defaultdict
from typing import Dict, List

# Хранилище для rate limiting: user_id -> list of timestamps
request_history: Dict[int, List[float]] = defaultdict(list)

# Лимиты: максимальное количество запросов в секунду
CLICK_LIMIT = 5          # для обычных кликов
BOSS_ATTACK_LIMIT = 3    # для атак на босса

# ==================== КОНФИГУРАЦИЯ ====================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('BOT_TOKEN')
if not TOKEN:
    raise ValueError("No BOT_TOKEN environment variable set")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("No DATABASE_URL environment variable set")

# Игровые константы
EXP_PER_LEVEL = 100
BASE_CLICK_REWARD = (3, 9)
BASE_EXP_REWARD = (1, 3)
MAX_RESOURCE_AMOUNT = 2_000_000_000  # защита от переполнения BIGINT

# ==================== МОДЕЛИ ДАННЫХ ====================

class Achievement:
    def __init__(self, id, name, desc, cond_func, reward_gold=0, reward_exp=0):
        self.id = id
        self.name = name
        self.description = desc
        self.condition_func = cond_func
        self.reward_gold = reward_gold
        self.reward_exp = reward_exp

# Улучшения
UPGRADES = {
    'click_power': {'name': '⚡ Сила клика', 'description': '+2 золота за клик', 'base_price': 50, 'price_mult': 2.0, 'effect': {'click_gold': 2}},
    'crit_chance': {'name': '🍀 Шанс крита', 'description': '+2% шанс двойной добычи', 'base_price': 100, 'price_mult': 1.5, 'effect': {'crit_chance': 2}}
}

# Ресурсы
RESOURCES = {
    'coal': {'name': 'Уголь', 'base_price': 5},
    'iron': {'name': 'Железо', 'base_price': 10},
    'gold': {'name': 'Золотая руда', 'base_price': 30},
    'diamond': {'name': 'Алмаз', 'base_price': 100},
    'mithril': {'name': 'Мифрил', 'base_price': 300},
    'soul_shard': {'name': 'Осколок души', 'base_price': 500},
    'dragon_scale': {'name': 'Чешуя дракона', 'base_price': 1000},
    'magic_essence': {'name': 'Эссенция магии', 'base_price': 2000}
}

# Локации (обычные)
LOCATIONS = {
    'coal_mine': {
        'name': 'Угольная шахта',
        'description': 'Мелкая шахта, много угля.',
        'min_level': 1,
        'min_tool_level': 0,
        'resources': [
            {'res_id': 'coal', 'prob': 0.8, 'min': 1, 'max': 3},
            {'res_id': 'iron', 'prob': 0.2, 'min': 1, 'max': 1}
        ]
    },
    'iron_mine': {
        'name': 'Железный рудник',
        'description': 'Залежи железной руды.',
        'min_level': 3,
        'min_tool_level': 0,
        'resources': [
            {'res_id': 'iron', 'prob': 0.7, 'min': 1, 'max': 2},
            {'res_id': 'coal', 'prob': 0.3, 'min': 1, 'max': 2},
            {'res_id': 'gold', 'prob': 0.1, 'min': 1, 'max': 1}
        ]
    },
    'gold_mine': {
        'name': 'Золотая жила',
        'description': 'Богатое месторождение золота.',
        'min_level': 5,
        'min_tool_level': 2,
        'resources': [
            {'res_id': 'gold', 'prob': 0.6, 'min': 1, 'max': 2},
            {'res_id': 'iron', 'prob': 0.3, 'min': 1, 'max': 2},
            {'res_id': 'diamond', 'prob': 0.1, 'min': 1, 'max': 1}
        ]
    },
    'diamond_cave': {
        'name': 'Алмазная пещера',
        'description': 'Редкие алмазы, опасно.',
        'min_level': 10,
        'min_tool_level': 3,
        'resources': [
            {'res_id': 'diamond', 'prob': 0.4, 'min': 1, 'max': 1},
            {'res_id': 'gold', 'prob': 0.4, 'min': 1, 'max': 2},
            {'res_id': 'mithril', 'prob': 0.2, 'min': 1, 'max': 1}
        ]
    },
    'mithril_mine': {
        'name': 'Мифриловые копи',
        'description': 'Древние копи.',
        'min_level': 20,
        'min_tool_level': 4,
        'resources': [
            {'res_id': 'mithril', 'prob': 0.5, 'min': 1, 'max': 2},
            {'res_id': 'diamond', 'prob': 0.3, 'min': 1, 'max': 1},
            {'res_id': 'gold', 'prob': 0.2, 'min': 1, 'max': 3}
        ]
    }
}

# Босс-локации
BOSS_LOCATIONS = {
    'goblin_king': {
        'name': 'Логово короля гоблинов',
        'description': 'Старый король гоблинов, накопивший горы золота.',
        'min_level': 5,
        'min_tool_level': 1,
        'boss': {
            'name': 'Король гоблинов',
            'health': 1000,
            'reward_gold': 5000,
            'reward_resources': {'soul_shard': (1, 3), 'gold': (10, 20)},
            'exp_reward': 500
        }
    },
    'dragon_lair': {
        'name': 'Логово дракона',
        'description': 'Древний дракон, стерегущий несметные сокровища.',
        'min_level': 5,
        'min_tool_level': 1,
        'boss': {
            'name': 'Огненный дракон',
            'health': 5000,
            'reward_gold': 20000,
            'reward_resources': {'dragon_scale': (1, 2), 'magic_essence': (2, 5)},
            'exp_reward': 2000
        }
    },
    'lich_castle': {
        'name': 'Цитадель лича',
        'description': 'Могущественный лич, собирающий души.',
        'min_level': 1,
        'min_tool_level': 1,
        'boss': {
            'name': 'Архилич',
            'health': 10000,
            'reward_gold': 50000,
            'reward_resources': {'soul_shard': (5, 10), 'magic_essence': (3, 7)},
            'exp_reward': 5000
        }
    }
}

# ... (после lich_castle)
BOSS_LOCATIONS.update({
    'elemental_core': {
        'name': 'Ядро элементаля',
        'description': 'Магматический элементаль, охраняющий редкие минералы.',
        'min_level': 10,
        'min_tool_level': 2,
        'boss': {
            'name': 'Пылающий элементаль',
            'health': 1500,
            'reward_gold': 8000,
            'reward_resources': {'soul_shard': (2, 4), 'magic_essence': (1, 2)},
            'exp_reward': 800
        }
    },
    'crystal_guardian': {
        'name': 'Хрустальный зал',
        'description': 'Гигантский голем из чистого кристалла.',
        'min_level': 15,
        'min_tool_level': 3,
        'boss': {
            'name': 'Кристальный страж',
            'health': 2500,
            'reward_gold': 12000,
            'reward_resources': {'diamond': (15, 25), 'dragon_scale': (1, 2)},
            'exp_reward': 1200
        }
    },
    'abyssal_serpent': {
        'name': 'Бездонная впадина',
        'description': 'Древний змей, обитающий в подземном озере.',
        'min_level': 20,
        'min_tool_level': 3,
        'boss': {
            'name': 'Глубинный змей',
            'health': 4000,
            'reward_gold': 18000,
            'reward_resources': {'magic_essence': (3, 6), 'dragon_scale': (2, 3)},
            'exp_reward': 1800
        }
    },
    'demon_lord': {
        'name': 'Инфернальная крепость',
        'description': 'Лорд демонов, жаждущий душ.',
        'min_level': 25,
        'min_tool_level': 4,
        'boss': {
            'name': 'Владыка демонов',
            'health': 6000,
            'reward_gold': 25000,
            'reward_resources': {'soul_shard': (8, 12), 'magic_essence': (5, 8)},
            'exp_reward': 2500
        }
    },
    'ice_queen': {
        'name': 'Ледяной дворец',
        'description': 'Королева вечной мерзлоты.',
        'min_level': 30,
        'min_tool_level': 4,
        'boss': {
            'name': 'Снежная королева',
            'health': 8000,
            'reward_gold': 35000,
            'reward_resources': {'diamond': (20, 30), 'dragon_scale': (3, 5)},
            'exp_reward': 3000
        }
    },
    'void_reaper': {
        'name': 'Пустота',
        'description': 'Жнец, пришедший из ниоткуда.',
        'min_level': 35,
        'min_tool_level': 5,
        'boss': {
            'name': 'Жнец душ',
            'health': 10000,
            'reward_gold': 50000,
            'reward_resources': {'soul_shard': (10, 15), 'magic_essence': (8, 12)},
            'exp_reward': 4000
        }
    },
    'ancient_colossus': {
        'name': 'Забытые руины',
        'description': 'Колосс, охраняющий древние сокровища.',
        'min_level': 40,
        'min_tool_level': 5,
        'boss': {
            'name': 'Каменный колосс',
            'health': 15000,
            'reward_gold': 80000,
            'reward_resources': {'mithril': (5, 10), 'dragon_scale': (5, 8)},
            'exp_reward': 6000
        }
    },
    'chaos_beast': {
        'name': 'Логово хаоса',
        'description': 'Зверь, искажающий реальность.',
        'min_level': 45,
        'min_tool_level': 5,
        'boss': {
            'name': 'Зверь хаоса',
            'health': 20000,
            'reward_gold': 120000,
            'reward_resources': {'magic_essence': (12, 18), 'soul_shard': (15, 20)},
            'exp_reward': 8000
        }
    },
    'time_wyrm': {
        'name': 'Временной разлом',
        'description': 'Дракон, живущий вне времени.',
        'min_level': 50,
        'min_tool_level': 5,
        'boss': {
            'name': 'Хроно-дракон',
            'health': 30000,
            'reward_gold': 200000,
            'reward_resources': {'dragon_scale': (10, 15), 'mithril': (8, 12)},
            'exp_reward': 10000
        }
    },
    'celestial_phoenix': {
        'name': 'Небесный чертог',
        'description': 'Феникс, возрождающийся в пламени.',
        'min_level': 60,
        'min_tool_level': 5,
        'boss': {
            'name': 'Небесный феникс',
            'health': 50000,
            'reward_gold': 500000,
            'reward_resources': {'magic_essence': (20, 30), 'dragon_scale': (15, 20), 'soul_shard': (20, 30)},
            'exp_reward': 20000
        }
    },
})

# Инструменты
TOOLS = {
    'wooden_pickaxe': {'name': 'Деревянная кирка', 'description': 'Самая простая.', 'price': 0, 'required_level': 1, 'base_power': 1, 'upgrade_cost': {'coal': 5, 'iron': 2}},
    'stone_pickaxe': {'name': 'Каменная кирка', 'description': 'Немного прочнее.', 'price': 100, 'required_level': 3, 'base_power': 2, 'upgrade_cost': {'coal': 10, 'iron': 5, 'gold': 1}},
    'iron_pickaxe': {'name': 'Железная кирка', 'description': 'Хорошая кирка.', 'price': 500, 'required_level': 5, 'base_power': 3, 'upgrade_cost': {'coal': 20, 'iron': 10, 'gold': 3}},
    'golden_pickaxe': {'name': 'Золотая кирка', 'description': 'Быстрая, но хрупкая.', 'price': 1000, 'required_level': 8, 'base_power': 2, 'upgrade_cost': {'coal': 30, 'iron': 15, 'gold': 10, 'diamond': 1}},
    'diamond_pickaxe': {'name': 'Алмазная кирка', 'description': 'Прочная и эффективная.', 'price': 5000, 'required_level': 15, 'base_power': 4, 'upgrade_cost': {'coal': 50, 'iron': 30, 'gold': 20, 'diamond': 5}},
    'mithril_pickaxe': {'name': 'Мифриловая кирка', 'description': 'Легендарная.', 'price': 20000, 'required_level': 25, 'base_power': 5, 'upgrade_cost': {'coal': 100, 'iron': 50, 'gold': 30, 'diamond': 10, 'mithril': 2}}
}

# Задания (шаблоны)
DAILY_TASK_TEMPLATES = [
    {'name': 'Труженик', 'description': 'Совершить {} кликов', 'goal': (50, 80), 'reward_gold': 70, 'reward_exp': 20},
    {'name': 'Золотоискатель', 'description': 'Заработать {} золота', 'goal': (100, 500), 'reward_gold': 100, 'reward_exp': 30},
    {'name': 'Покупатель', 'description': 'Купить улучшений на {} золота', 'goal': (150, 300), 'reward_gold': 80, 'reward_exp': 25},
    {'name': 'Везунчик', 'description': 'Получить {} критических ударов', 'goal': (3, 8), 'reward_gold': 70, 'reward_exp': 40},
    {'name': 'Рудокоп', 'description': 'Добыть {} ресурсов', 'goal': (5, 15), 'reward_gold': 60, 'reward_exp': 35},
    {'name': 'Продавец', 'description': 'Продать ресурсов на {} золота', 'goal': (200, 500), 'reward_gold': 90, 'reward_exp': 45},
    {'name': 'Ударник труда', 'description': 'Совершить {} кликов', 'goal': (80, 120), 'reward_gold': 90, 'reward_exp': 30},
    {'name': 'Золотая жила', 'description': 'Заработать {} золота', 'goal': (500, 1000), 'reward_gold': 150, 'reward_exp': 50},
    {'name': 'Транжира', 'description': 'Купить улучшений на {} золота', 'goal': (300, 600), 'reward_gold': 120, 'reward_exp': 40},
    {'name': 'Счастливчик', 'description': 'Получить {} критических ударов', 'goal': (8, 15), 'reward_gold': 100, 'reward_exp': 60},
    {'name': 'Горняк', 'description': 'Добыть {} ресурсов', 'goal': (15, 30), 'reward_gold': 90, 'reward_exp': 45},
    {'name': 'Торговый магнат', 'description': 'Продать ресурсов на {} золота', 'goal': (500, 1000), 'reward_gold': 150, 'reward_exp': 70},
]

WEEKLY_TASK_TEMPLATES = [
    {'name': 'Шахтёр-неделя', 'description': 'Совершить {} кликов', 'goal': (400, 800), 'reward_gold': 500, 'reward_exp': 200},
    {'name': 'Золотая лихорадка', 'description': 'Заработать {} золота', 'goal': (2000, 5000), 'reward_gold': 1000, 'reward_exp': 500},
    {'name': 'Магнат', 'description': 'Купить улучшений на {} золота', 'goal': (1500, 3000), 'reward_gold': 800, 'reward_exp': 400},
    {'name': 'Критический удар', 'description': 'Получить {} критических ударов', 'goal': (20, 50), 'reward_gold': 600, 'reward_exp': 300},
    {'name': 'Коллекционер', 'description': 'Добыть {} ресурсов', 'goal': (50, 150), 'reward_gold': 700, 'reward_exp': 350},
    {'name': 'Торговец', 'description': 'Продать ресурсов на {} золота', 'goal': (2000, 5000), 'reward_gold': 900, 'reward_exp': 450},
    {'name': 'Шахтёр-профи', 'description': 'Совершить {} кликов', 'goal': (800, 1300), 'reward_gold': 1000, 'reward_exp': 400},
    {'name': 'Золотой дождь', 'description': 'Заработать {} золота', 'goal': (5000, 10000), 'reward_gold': 2000, 'reward_exp': 1000},
    {'name': 'Олигарх', 'description': 'Купить улучшений на {} золота', 'goal': (3000, 6000), 'reward_gold': 1500, 'reward_exp': 800},
    {'name': 'Крит-мастер', 'description': 'Получить {} критических ударов', 'goal': (50, 100), 'reward_gold': 1200, 'reward_exp': 600},
    {'name': 'Скряга', 'description': 'Добыть {} ресурсов', 'goal': (150, 300), 'reward_gold': 1400, 'reward_exp': 700},
    {'name': 'Биржевой игрок', 'description': 'Продать ресурсов на {} золота', 'goal': (5000, 10000), 'reward_gold': 1800, 'reward_exp': 900},
]

FAQ = [
    {"question": "🪨 Как добывать ресурсы?", "answer": "Нажимай кнопку «⛏ Добыть» в главном меню. Каждый клик приносит золото, опыт и случайные ресурсы в зависимости от текущей локации."},
    {"question": "🗺 Как открыть новые локации?", "answer": "Повышай уровень, кликая. Каждая новая локация требует определённый уровень. Начиная с Золотой жилы, также требуется минимальный уровень активного инструмента. Список доступных локаций можно посмотреть по команде /locations."},
    {"question": "🧰 Зачем нужны инструменты?", "answer": "Инструменты (кирки) увеличивают количество добываемых ресурсов. Их можно купить в магазине за золото, а затем улучшать за ресурсы. Чем выше уровень инструмента, тем больше ресурсов ты добываешь за клик."},
    {"question": "📋 Что такое ежедневные и еженедельные задания?", "answer": "Каждый день появляются 3 случайных задания, а каждую неделю – 2 более сложных. Выполняй их, чтобы получать дополнительное золото и опыт."},
    {"question": "💰 Как продать ресурсы?", "answer": "Зайди в раздел «💰 Рынок» (команда /market). Ты увидишь список своих ресурсов и текущие цены. Можно продать 1 единицу или всё количество сразу."},
    {"question": "🏆 Что такое достижения?", "answer": "Достижения – это особые цели, за выполнение которых даются награды (золото и опыт). Посмотреть список своих достижений можно по команде /achievements."},
    {"question": "⚡ Как увеличить доход за клик?", "answer": "Покупай улучшения в магазине (категория «⚡ Улучшения»). «Сила клика» прямо увеличивает золото за клик, а «Шанс крита» даёт шанс удвоить добычу."},
    {"question": "🔄 Как сменить активный инструмент?", "answer": "В магазине в категории «🧰 Инструменты» нажми кнопку «🔨 Сделать активным» рядом с нужным инструментом. Активный инструмент используется при добыче."},
    {"question": "🔨 Что такое крафт?", "answer": "В разделе «Крафт» ты можешь создавать полезные предметы из ресурсов: зелья, ключи для повторного боя с боссами, модификаторы для инструментов и конвертировать ресурсы."},
]

# ==================== УСЛОВИЯ ДОСТИЖЕНИЙ ====================

def cond_first_click(uid, data): stats = data['stats']; return stats['clicks'] >= 1, stats['clicks'], 1
def cond_clicks_100(uid, data): stats = data['stats']; return stats['clicks'] >= 100, stats['clicks'], 100
def condition_clicks_300(uid, data): stats = data['stats']; return stats['clicks'] >= 300, stats['clicks'], 300
def condition_clicks_500(uid, data): stats = data['stats']; return stats['clicks'] >= 500, stats['clicks'], 500
def condition_clicks_1000(uid, data): stats = data['stats']; return stats['clicks'] >= 1000, stats['clicks'], 1000
def cond_gold_1000(uid, data): stats = data['stats']; return stats['total_gold'] >= 1000, stats['total_gold'], 1000
def condition_gold_1500(uid, data): stats = data['stats']; return stats['total_gold'] >= 1500, stats['total_gold'], 1500
def condition_gold_5000(uid, data): stats = data['stats']; return stats['total_gold'] >= 5000, stats['total_gold'], 5000
def condition_gold_20000(uid, data): stats = data['stats']; return stats['total_gold'] >= 20000, stats['total_gold'], 20000
def cond_resources_50(uid, data): return data['inv_total'] >= 50, data['inv_total'], 50
def condition_collector_all(uid, data):
    inv = data['inv']
    min_amount = min(inv.get(rid, 0) for rid in RESOURCES)
    return min_amount >= 100, min_amount, 100
def cond_crits_50(uid, data): stats = data['stats']; return stats['total_crits'] >= 50, stats['total_crits'], 50
def condition_crit_master(uid, data): stats = data['stats']; return stats['total_crits'] >= 100, stats['total_crits'], 100
def cond_crit_streak_5(uid, data): stats = data['stats']; return stats['max_crit_streak'] >= 5, stats['max_crit_streak'], 5
def condition_smith(uid, data):
    tools = data['tools']
    max_level = max(tools.values()) if tools else 0
    return max_level >= 5, max_level, 5
def condition_tool_master(uid, data):
    tools = data['tools']
    all_tools = list(TOOLS.keys())
    min_level = min(tools.get(tid, 0) for tid in all_tools)
    return min_level >= 3, min_level, 3
def condition_tools_all_purchased(uid, data):
    tools = data['tools']
    all_tools = list(TOOLS.keys())
    purchased = [tid for tid in all_tools if tid in tools]
    return len(purchased) == len(all_tools), len(purchased), len(all_tools)
def condition_tools_all_level5(uid, data):
    tools = data['tools']
    all_tools = list(TOOLS.keys())
    if len(tools) != len(all_tools):
        return False, len(tools), len(all_tools)
    for tid in all_tools:
        if tools.get(tid, 0) < 5:
            return False, tools.get(tid, 0), 5
    return True, 5, 5
def condition_tools_total_level_50(uid, data):
    tools = data['tools']
    total = sum(tools.values())
    return total >= 50, total, 50
def condition_tools_total_level_100(uid, data):
    tools = data['tools']
    total = sum(tools.values())
    return total >= 100, total, 100
def condition_hardworker(uid, data):
    total = data['daily_completed'] + data['weekly_completed']
    return total >= 50, total, 50
def condition_explorer(uid, data):
    stats = data['stats']
    max_loc_level = max(loc['min_level'] for loc in LOCATIONS.values())
    return stats['level'] >= max_loc_level, stats['level'], max_loc_level

ACHIEVEMENTS = [
    Achievement('first_click', 'Первые шаги', 'Сделать первый клик', cond_first_click, 10, 5),
    Achievement('clicks_100', 'Начинающий шахтёр', 'Сделать 100 кликов', cond_clicks_100, 50, 20),
    Achievement('clicks_300', 'Трудоголик', 'Сделать 300 кликов', condition_clicks_300, 80, 35),
    Achievement('clicks_500', 'Опытный шахтёр', 'Сделать 500 кликов', condition_clicks_500, 120, 50),
    Achievement('clicks_1000', 'Ветеран', 'Сделать 1000 кликов', condition_clicks_1000, 200, 100),
    Achievement('gold_1000', 'Золотая жила', 'Добыть 1000 золота', cond_gold_1000, 100, 50),
    Achievement('gold_1500', 'Золотая лихорадка', 'Добыть 1500 золота', condition_gold_1500, 150, 75),
    Achievement('gold_5000', 'Золотой магнат', 'Добыть 5000 золота', condition_gold_5000, 300, 150),
    Achievement('gold_20000', 'Король золота', 'Добыть 20000 золота', condition_gold_20000, 600, 300),
    Achievement('resources_50', 'Коллекционер', 'Собрать 50 любых ресурсов', cond_resources_50, 70, 35),
    Achievement('collector_all', 'Абсолютный коллекционер', 'Собрать не менее 100 каждого ресурса', condition_collector_all, 400, 200),
    Achievement('crits_50', 'Критическая масса', 'Получить 50 критических ударов', cond_crits_50, 80, 30),
    Achievement('crit_master', 'Критический удар', 'Получить 100 критических ударов', condition_crit_master, 250, 120),
    Achievement('crit_streak_5', 'Везунчик', 'Достичь серии критов в 5', cond_crit_streak_5, 60, 25),
    Achievement('smith', 'Кузнец', 'Улучшить любой инструмент до 5 уровня', condition_smith, 150, 50),
    Achievement('tool_master', 'Мастер инструментов', 'Все инструменты минимум 3 уровня', condition_tool_master, 350, 180),
    Achievement('tools_all_purchased', 'Коллекционер инструментов', 'Купить все виды кирок', condition_tools_all_purchased, 200, 100),
    Achievement('tools_all_level5', 'Легендарный кузнец', 'Все инструменты 5 уровня', condition_tools_all_level5, 500, 250),
    Achievement('tools_total_50', 'Сила инструментов I', 'Суммарный уровень инструментов 50', condition_tools_total_level_50, 150, 60),
    Achievement('tools_total_100', 'Сила инструментов II', 'Суммарный уровень инструментов 100', condition_tools_total_level_100, 300, 150),
    Achievement('hardworker', 'Трудяга', 'Выполнить 50 заданий', condition_hardworker, 200, 100),
    Achievement('explorer', 'Исследователь', 'Достичь максимального уровня', condition_explorer, 300, 150),
]

# ==================== ГЛОБАЛЬНЫЙ ПУЛ БД ====================

db_pool: Optional[asyncpg.Pool] = None

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_week_number(d=None):
    if d is None:
        d = datetime.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-{w:02d}"

def get_upgrade_cost(tid: str, level: int) -> dict:
    if level == 0:
        return {}
    base_cost = TOOLS[tid]['upgrade_cost']
    return {res: amount * level for res, amount in base_cost.items()}

def get_tool_power(uid: int, tid: str, level: int) -> int:
    if level == 0:
        return 0
    return TOOLS[tid]['base_power'] + level - 1

def get_click_reward(stats: dict) -> Tuple[int, int, bool]:
    cpl = stats['upgrades']['click_power']
    ccl = stats['upgrades']['crit_chance'] + stats.get('perm_crit_bonus', 0)  # добавляем постоянный бонус
    bg = random.randint(*BASE_CLICK_REWARD)
    be = random.randint(*BASE_EXP_REWARD)
    gold = bg + cpl * 2
    crit = (ccl * 2) / 100.0
    is_crit = random.random() < crit
    if is_crit:
        gold *= 2
        be *= 2
    return gold, be, is_crit

async def reply_or_edit(update_or_query, text: str, reply_markup=None, parse_mode=None):
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        try:
            await update_or_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise

# ==================== ФУНКЦИИ БАЗЫ ДАННЫХ (с поддержкой переданного соединения) ====================

async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS players (
                user_id BIGINT PRIMARY KEY,
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
                last_weekly_reset TEXT,
                current_location TEXT DEFAULT 'coal_mine',
                active_tool TEXT DEFAULT 'wooden_pickaxe'
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS upgrades (
                user_id BIGINT,
                upgrade_id TEXT,
                level INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, upgrade_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS daily_tasks (
                user_id BIGINT,
                task_id INTEGER,
                task_name TEXT,
                description TEXT,
                goal INTEGER,
                progress INTEGER DEFAULT 0,
                completed BOOLEAN DEFAULT FALSE,
                reward_gold INTEGER,
                reward_exp INTEGER,
                date DATE,
                PRIMARY KEY (user_id, task_id, date)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS weekly_tasks (
                user_id BIGINT,
                task_id INTEGER,
                task_name TEXT,
                description TEXT,
                goal INTEGER,
                progress INTEGER DEFAULT 0,
                completed BOOLEAN DEFAULT FALSE,
                reward_gold INTEGER,
                reward_exp INTEGER,
                week TEXT,
                PRIMARY KEY (user_id, task_id, week)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_achievements (
                user_id BIGINT,
                achievement_id TEXT,
                unlocked_at DATE,
                progress INTEGER,
                max_progress INTEGER,
                PRIMARY KEY (user_id, achievement_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                user_id BIGINT,
                resource_id TEXT,
                amount INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, resource_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS player_tools (
                user_id BIGINT,
                tool_id TEXT,
                level INTEGER DEFAULT 1,
                experience INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, tool_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS boss_progress (
                user_id BIGINT,
                boss_id TEXT,
                current_health INTEGER,
                defeated BOOLEAN DEFAULT FALSE,
                last_attempt TIMESTAMP,
                PRIMARY KEY (user_id, boss_id)
            )
        ''')
        # Таблица для предметов крафта
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS player_items (
                user_id BIGINT,
                item_id TEXT,
                quantity INTEGER DEFAULT 1,
                expires_at TIMESTAMP,
                PRIMARY KEY (user_id, item_id)
            )
        ''')
        # Таблица для глобального состояния (автосброс боссов)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS global_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                last_boss_reset TIMESTAMP
            )
        ''')
                # Таблица для активных эффектов (зелья и т.п.)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS active_effects (
                user_id BIGINT,
                effect_id TEXT,
                expires_at TIMESTAMP,
                effect_data JSONB,
                PRIMARY KEY (user_id, effect_id)
            )
        ''')
        # Добавляем колонки для постоянных бонусов в таблицу players, если их ещё нет
        await conn.execute('''
            ALTER TABLE players
            ADD COLUMN IF NOT EXISTS perm_tool_power_bonus INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS perm_crit_bonus INTEGER DEFAULT 0
        ''')
        # Инициализация global_state, если нет записи
        await conn.execute('''
            INSERT INTO global_state (id, last_boss_reset)
            SELECT 1, NOW() WHERE NOT EXISTS (SELECT 1 FROM global_state WHERE id = 1)
        ''')
        logger.info("Database tables initialized (if not existed)")

# ---------- Игроки ----------
async def get_player(uid: int, username: str = None, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        row = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1", uid)
        if not row:
            today = datetime.date.today()
            cur_week = get_week_number()
            await conn.execute(
                "INSERT INTO players (user_id, username, last_daily_reset, last_weekly_reset) VALUES ($1, $2, $3, $4)",
                uid, username, today, cur_week
            )
            for up_id in UPGRADES:
                await conn.execute(
                    "INSERT INTO upgrades (user_id, upgrade_id, level) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING",
                    uid, up_id
                )
            for rid in RESOURCES:
                await conn.execute(
                    "INSERT INTO inventory (user_id, resource_id, amount) VALUES ($1, $2, 0) ON CONFLICT DO NOTHING",
                    uid, rid
                )
            await conn.execute(
                "INSERT INTO player_tools (user_id, tool_id, level, experience) VALUES ($1, $2, 1, 0) ON CONFLICT DO NOTHING",
                uid, 'wooden_pickaxe'
            )
            await generate_daily_tasks(uid, conn)
            await generate_weekly_tasks(uid, conn)
            row = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1", uid)
        return dict(row)

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def get_player_stats(uid: int, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        row = await conn.fetchrow(
            "SELECT level, exp, gold, total_clicks, total_gold_earned, total_crits, current_crit_streak, max_crit_streak, perm_tool_power_bonus, perm_crit_bonus FROM players WHERE user_id = $1",
            uid
        )
        if not row:
            return {}
        lvl, exp, gold, clicks, tg, crits, cstreak, mstreak, perm_tool_bonus, perm_crit_bonus = row
        ups = {}
        for up_id in UPGRADES:
            level = await conn.fetchval("SELECT level FROM upgrades WHERE user_id = $1 AND upgrade_id = $2", uid, up_id)
            ups[up_id] = level if level is not None else 0
        total_exp = (lvl - 1) * EXP_PER_LEVEL + exp
        return {
            'level': lvl, 
            'exp': exp, 
            'total_exp': total_exp,
            'exp_next': EXP_PER_LEVEL,
            'gold': gold, 
            'clicks': clicks, 
            'total_gold': tg,
            'total_crits': crits, 
            'current_crit_streak': cstreak,
            'max_crit_streak': mstreak, 
            'upgrades': ups,
            'perm_tool_power_bonus': perm_tool_bonus,
            'perm_crit_bonus': perm_crit_bonus
        }

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def update_player(uid: int, conn: asyncpg.Connection = None, **kwargs):
    if not kwargs:
        return
    set_clause = ', '.join([f"{k} = ${i+2}" for i, k in enumerate(kwargs.keys())])
    values = list(kwargs.values())
    async def _update(conn):
        await conn.execute(f"UPDATE players SET {set_clause} WHERE user_id = $1", uid, *values)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _update(conn)
    else:
        await _update(conn)

async def level_up_if_needed(uid: int, conn: asyncpg.Connection = None):
    async def _level(conn):
        row = await conn.fetchrow("SELECT level, exp FROM players WHERE user_id = $1", uid)
        lvl, exp = row['level'], row['exp']
        while exp >= EXP_PER_LEVEL:
            lvl += 1
            exp -= EXP_PER_LEVEL
        await conn.execute("UPDATE players SET level = $1, exp = $2 WHERE user_id = $3", lvl, exp, uid)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _level(conn)
    else:
        await _level(conn)

# ---------- Улучшения ----------
async def purchase_upgrade(uid: int, upgrade_id: str, conn: asyncpg.Connection = None) -> Tuple[bool, str, int]:
    async def _purchase(conn):
        async with conn.transaction():
            row = await conn.fetchrow("SELECT level FROM upgrades WHERE user_id=$1 AND upgrade_id=$2", uid, upgrade_id)
            if not row:
                return False, "Улучшение не найдено", 0
            level = row['level']
            price = int(UPGRADES[upgrade_id]['base_price'] * (UPGRADES[upgrade_id]['price_mult'] ** level))
            gold = await conn.fetchval("SELECT gold FROM players WHERE user_id=$1", uid)
            if gold < price:
                logger.warning(f"User {uid} attempted to buy {upgrade_id} but insufficient gold: {gold} < {price}")
                return False, "❌ Недостаточно золота!", level
            await conn.execute("UPDATE players SET gold = gold - $1 WHERE user_id=$2", price, uid)
            await conn.execute("UPDATE upgrades SET level = level + 1 WHERE user_id=$1 AND upgrade_id=$2", uid, upgrade_id)
        new_level = level + 1
        return True, f"✅ {UPGRADES[upgrade_id]['name']} улучшен до {new_level} уровня.", new_level

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _purchase(conn)
    else:
        return await _purchase(conn)

# ---------- Задания ----------
async def generate_daily_tasks(uid: int, conn: asyncpg.Connection = None):
    async def _gen(conn):
        today = datetime.date.today()
        await conn.execute("DELETE FROM daily_tasks WHERE user_id = $1 AND date = $2", uid, today)
        templates = random.sample(DAILY_TASK_TEMPLATES, min(4, len(DAILY_TASK_TEMPLATES)))
        for i, t in enumerate(templates):
            goal = random.randint(*t['goal'])
            desc = t['description'].format(goal)
            await conn.execute(
                "INSERT INTO daily_tasks (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, date) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                uid, i, t['name'], desc, goal, t['reward_gold'], t['reward_exp'], today
            )
    if conn:
        await _gen(conn)
    else:
        async with db_pool.acquire() as conn:
            await _gen(conn)

async def check_daily_reset(uid: int, conn: asyncpg.Connection = None) -> bool:
    async def _check(conn):
        last = await conn.fetchval("SELECT last_daily_reset FROM players WHERE user_id = $1", uid)
        today = datetime.date.today()
        if last != today:
            await generate_daily_tasks(uid, conn)
            await conn.execute("UPDATE players SET last_daily_reset = $1 WHERE user_id = $2", today, uid)
            return True
        return False

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _check(conn)
    else:
        return await _check(conn)

async def get_daily_tasks(uid: int, conn: asyncpg.Connection = None) -> list:
    async def _get(conn):
        today = datetime.date.today()
        rows = await conn.fetch(
            "SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp FROM daily_tasks WHERE user_id = $1 AND date = $2",
            uid, today
        )
        return [list(row) for row in rows]

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def update_daily_task_progress(uid: int, task_type: str, delta: int, conn: asyncpg.Connection = None):
    async def _update(conn):
        today = datetime.date.today()
        await conn.execute(
            "UPDATE daily_tasks SET progress = progress + $1 WHERE user_id = $2 AND date = $3 AND completed = FALSE AND task_name LIKE $4",
            delta, uid, today, f'%{task_type}%'
        )
        rows = await conn.fetch(
            "SELECT task_id, goal, reward_gold, reward_exp FROM daily_tasks WHERE user_id = $1 AND date = $2 AND completed = FALSE",
            uid, today
        )
        for task_id, goal, rg, re in rows:
            prog = await conn.fetchval(
                "SELECT progress FROM daily_tasks WHERE user_id = $1 AND task_id = $2 AND date = $3",
                uid, task_id, today
            )
            if prog >= goal:
                await conn.execute(
                    "UPDATE daily_tasks SET completed = TRUE WHERE user_id = $1 AND task_id = $2 AND date = $3",
                    uid, task_id, today
                )
                await conn.execute(
                    "UPDATE players SET gold = gold + $1, exp = exp + $2 WHERE user_id = $3",
                    rg, re, uid
                )
                await level_up_if_needed(uid, conn)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _update(conn)
    else:
        await _update(conn)

async def generate_weekly_tasks(uid: int, conn: asyncpg.Connection = None):
    async def _gen(conn):
        week = get_week_number()
        await conn.execute("DELETE FROM weekly_tasks WHERE user_id = $1 AND week = $2", uid, week)
        templates = random.sample(WEEKLY_TASK_TEMPLATES, min(4, len(WEEKLY_TASK_TEMPLATES)))
        for i, t in enumerate(templates):
            goal = random.randint(*t['goal'])
            desc = t['description'].format(goal)
            await conn.execute(
                "INSERT INTO weekly_tasks (user_id, task_id, task_name, description, goal, reward_gold, reward_exp, week) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                uid, i, t['name'], desc, goal, t['reward_gold'], t['reward_exp'], week
            )
    if conn:
        await _gen(conn)
    else:
        async with db_pool.acquire() as conn:
            await _gen(conn)

async def check_weekly_reset(uid: int, conn: asyncpg.Connection = None) -> bool:
    async def _check(conn):
        last = await conn.fetchval("SELECT last_weekly_reset FROM players WHERE user_id = $1", uid)
        cur = get_week_number()
        if last != cur:
            await generate_weekly_tasks(uid, conn)
            await conn.execute("UPDATE players SET last_weekly_reset = $1 WHERE user_id = $2", cur, uid)
            return True
        return False

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _check(conn)
    else:
        return await _check(conn)

async def get_weekly_tasks(uid: int, conn: asyncpg.Connection = None) -> list:
    async def _get(conn):
        week = get_week_number()
        rows = await conn.fetch(
            "SELECT task_id, task_name, description, goal, progress, completed, reward_gold, reward_exp FROM weekly_tasks WHERE user_id = $1 AND week = $2",
            uid, week
        )
        return [list(row) for row in rows]

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def update_weekly_task_progress(uid: int, task_type: str, delta: int, conn: asyncpg.Connection = None):
    async def _update(conn):
        week = get_week_number()
        await conn.execute(
            "UPDATE weekly_tasks SET progress = progress + $1 WHERE user_id = $2 AND week = $3 AND completed = FALSE AND task_name LIKE $4",
            delta, uid, week, f'%{task_type}%'
        )
        rows = await conn.fetch(
            "SELECT task_id, goal, reward_gold, reward_exp FROM weekly_tasks WHERE user_id = $1 AND week = $2 AND completed = FALSE",
            uid, week
        )
        for task_id, goal, rg, re in rows:
            prog = await conn.fetchval(
                "SELECT progress FROM weekly_tasks WHERE user_id = $1 AND task_id = $2 AND week = $3",
                uid, task_id, week
            )
            if prog >= goal:
                await conn.execute(
                    "UPDATE weekly_tasks SET completed = TRUE WHERE user_id = $1 AND task_id = $2 AND week = $3",
                    uid, task_id, week
                )
                await conn.execute(
                    "UPDATE players SET gold = gold + $1, exp = exp + $2 WHERE user_id = $3",
                    rg, re, uid
                )
                await level_up_if_needed(uid, conn)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _update(conn)
    else:
        await _update(conn)

# ---------- Инвентарь ----------
async def get_inventory(uid: int, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        rows = await conn.fetch("SELECT resource_id, amount FROM inventory WHERE user_id = $1", uid)
        return {row['resource_id']: row['amount'] for row in rows}

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def add_resource(uid: int, rid: str, amt: int = 1, conn: asyncpg.Connection = None) -> bool:
    async def _add(conn):
        current = await conn.fetchval("SELECT amount FROM inventory WHERE user_id=$1 AND resource_id=$2", uid, rid)
        if current is None:
            current = 0
        new_amount = current + amt
        if new_amount > MAX_RESOURCE_AMOUNT:
            new_amount = MAX_RESOURCE_AMOUNT
            if new_amount <= current:
                return False
        await conn.execute("""
            INSERT INTO inventory (user_id, resource_id, amount)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, resource_id) DO UPDATE
            SET amount = $3
        """, uid, rid, new_amount)
        logger.debug(f"add_resource: user={uid}, res={rid}, added {amt}, new total={new_amount}")
        return True

    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _add(conn)
    else:
        return await _add(conn)

async def remove_resource(uid: int, rid: str, amt: int = 1, conn: asyncpg.Connection = None) -> bool:
    async def _remove(conn):
        current = await conn.fetchval("SELECT amount FROM inventory WHERE user_id=$1 AND resource_id=$2", uid, rid)
        if current is None or current < amt:
            return False
        await conn.execute("UPDATE inventory SET amount = amount - $1 WHERE user_id=$2 AND resource_id=$3", amt, uid, rid)
        return True

    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _remove(conn)
    else:
        return await _remove(conn)

# ---------- Инструменты ----------
async def get_player_tools(uid: int, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        rows = await conn.fetch("SELECT tool_id, level FROM player_tools WHERE user_id = $1", uid)
        return {row['tool_id']: row['level'] for row in rows}

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def add_tool(uid: int, tid: str, conn: asyncpg.Connection = None):
    async def _add(conn):
        await conn.execute("INSERT INTO player_tools (user_id, tool_id, level, experience) VALUES ($1, $2, 1, 0) ON CONFLICT DO NOTHING", uid, tid)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _add(conn)
    else:
        await _add(conn)

async def has_tool(uid: int, tid: str, conn: asyncpg.Connection = None) -> bool:
    async def _has(conn):
        val = await conn.fetchval("SELECT 1 FROM player_tools WHERE user_id = $1 AND tool_id = $2", uid, tid)
        return val is not None

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _has(conn)
    else:
        return await _has(conn)

async def get_tool_level(uid: int, tid: str, conn: asyncpg.Connection = None) -> int:
    async def _get(conn):
        level = await conn.fetchval("SELECT level FROM player_tools WHERE user_id = $1 AND tool_id = $2", uid, tid)
        return level if level is not None else 0

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def can_upgrade_tool(uid: int, tid: str, conn: asyncpg.Connection = None) -> bool:
    async def _can(conn):
        level = await get_tool_level(uid, tid, conn)
        if level == 0:
            return False
        cost = get_upgrade_cost(tid, level)
        inv = await get_inventory(uid, conn)
        for res, need in cost.items():
            if inv.get(res, 0) < need:
                return False
        return True

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _can(conn)
    else:
        return await _can(conn)

async def upgrade_tool(uid: int, tid: str, conn: asyncpg.Connection = None) -> bool:
    async def _upgrade(conn):
        if not await can_upgrade_tool(uid, tid, conn):
            return False
        level = await get_tool_level(uid, tid, conn)
        cost = get_upgrade_cost(tid, level)
        async with conn.transaction():
            for res, need in cost.items():
                await conn.execute("UPDATE inventory SET amount = amount - $1 WHERE user_id = $2 AND resource_id = $3", need, uid, res)
            await conn.execute("UPDATE player_tools SET level = level + 1 WHERE user_id = $1 AND tool_id = $2", uid, tid)
        return True

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _upgrade(conn)
    else:
        return await _upgrade(conn)

async def get_active_tool(uid: int, conn: asyncpg.Connection = None) -> str:
    async def _get(conn):
        tool = await conn.fetchval("SELECT active_tool FROM players WHERE user_id = $1", uid)
        return tool if tool else 'wooden_pickaxe'

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def get_active_tool_level(uid: int, conn: asyncpg.Connection = None) -> int:
    active = await get_active_tool(uid, conn)
    return await get_tool_level(uid, active, conn)

async def set_active_tool(uid: int, tid: str, conn: asyncpg.Connection = None):
    async def _set(conn):
        await conn.execute("UPDATE players SET active_tool = $1 WHERE user_id = $2", tid, uid)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _set(conn)
    else:
        await _set(conn)

# ---------- Локации ----------
async def get_player_current_location(uid: int, conn: asyncpg.Connection = None) -> str:
    async def _get(conn):
        loc = await conn.fetchval("SELECT current_location FROM players WHERE user_id = $1", uid)
        return loc if loc else 'coal_mine'

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def set_player_location(uid: int, loc: str, conn: asyncpg.Connection = None):
    async def _set(conn):
        await conn.execute("UPDATE players SET current_location = $1 WHERE user_id = $2", loc, uid)

    if conn is None:
        async with db_pool.acquire() as conn:
            await _set(conn)
    else:
        await _set(conn)

# ---------- Боссы ----------
async def get_boss_progress(uid: int, boss_id: str, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        row = await conn.fetchrow("SELECT current_health, defeated FROM boss_progress WHERE user_id=$1 AND boss_id=$2", uid, boss_id)
        if not row:
            health = BOSS_LOCATIONS[boss_id]['boss']['health']
            await conn.execute("INSERT INTO boss_progress (user_id, boss_id, current_health) VALUES ($1, $2, $3)", uid, boss_id, health)
            return {'current_health': health, 'defeated': False}
        return dict(row)

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def update_boss_health(uid: int, boss_id: str, damage: int, conn: asyncpg.Connection = None) -> bool:
    async def _update(conn):
        await conn.execute("SELECT current_health FROM boss_progress WHERE user_id=$1 AND boss_id=$2 FOR UPDATE", uid, boss_id)
        await conn.execute("UPDATE boss_progress SET current_health = current_health - $1 WHERE user_id=$2 AND boss_id=$3 AND current_health > 0", damage, uid, boss_id)
        row = await conn.fetchrow("SELECT current_health FROM boss_progress WHERE user_id=$1 AND boss_id=$2", uid, boss_id)
        if row['current_health'] <= 0:
            await conn.execute("UPDATE boss_progress SET defeated=TRUE, current_health=0 WHERE user_id=$1 AND boss_id=$2", uid, boss_id)
            return True
        return False

    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _update(conn)
    else:
        return await _update(conn)

async def check_and_reset_bosses(conn: asyncpg.Connection):
    row = await conn.fetchrow("SELECT last_boss_reset FROM global_state WHERE id = 1")
    if not row:
        await conn.execute("INSERT INTO global_state (id, last_boss_reset) VALUES (1, NOW())")
        return
    last_reset = row['last_boss_reset']
    now = datetime.datetime.now()
    if last_reset is None or (now - last_reset) > datetime.timedelta(hours=6):
        for boss_id, bloc in BOSS_LOCATIONS.items():
            max_hp = bloc['boss']['health']
            await conn.execute("""
                UPDATE boss_progress
                SET current_health = $1, defeated = false
                WHERE boss_id = $2
            """, max_hp, boss_id)
        await conn.execute("UPDATE global_state SET last_boss_reset = $1 WHERE id = 1", now)
        logger.info(f"Bosses reset at {now}")

# ---------- Достижения ----------
async def get_achievements_data(uid: int, conn: asyncpg.Connection = None) -> Tuple[set, int, int]:
    async def _get(conn):
        unlocked_rows = await conn.fetch("SELECT achievement_id FROM user_achievements WHERE user_id = $1", uid)
        unlocked = {row['achievement_id'] for row in unlocked_rows}
        daily_completed = await conn.fetchval("SELECT COUNT(*) FROM daily_tasks WHERE user_id = $1 AND completed = TRUE", uid) or 0
        weekly_completed = await conn.fetchval("SELECT COUNT(*) FROM weekly_tasks WHERE user_id = $1 AND completed = TRUE", uid) or 0
        return unlocked, daily_completed, weekly_completed

    if conn is None:
        async with db_pool.acquire() as conn:
            return await _get(conn)
    else:
        return await _get(conn)

async def unlock_achievement(uid: int, ach_id: str, gold: int, exp: int, progress: int, max_progress: int, conn: asyncpg.Connection = None):
    today = datetime.date.today()
    async def _unlock(conn):
        await conn.execute(
            "INSERT INTO user_achievements (user_id, achievement_id, unlocked_at, progress, max_progress) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
            uid, ach_id, today, progress, max_progress
        )
        await conn.execute(
            "UPDATE players SET gold = gold + $1, exp = exp + $2 WHERE user_id = $3",
            gold, exp, uid
        )
        await level_up_if_needed(uid, conn)

    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await _unlock(conn)
    else:
        await _unlock(conn)

def evaluate_achievement(ach: Achievement, uid: int, data: dict) -> tuple[bool, int, int]:
    return ach.condition_func(uid, data)

async def check_achievements(uid: int, ctx: ContextTypes.DEFAULT_TYPE = None, conn: asyncpg.Connection = None):
    stats = await get_player_stats(uid, conn)
    inv = await get_inventory(uid, conn)
    inv_total = sum(inv.values())
    tools = await get_player_tools(uid, conn)
    unlocked, daily_completed, weekly_completed = await get_achievements_data(uid, conn)

    data = {
        'stats': stats,
        'inv_total': inv_total,
        'inv': inv,
        'tools': tools,
        'daily_completed': daily_completed,
        'weekly_completed': weekly_completed
    }

    new_ach = []
    for ach in ACHIEVEMENTS:
        if ach.id in unlocked:
            continue
        achieved, prog, maxp = evaluate_achievement(ach, uid, data)
        if achieved:
            await unlock_achievement(uid, ach.id, ach.reward_gold, ach.reward_exp, prog, maxp, conn)
            new_ach.append(ach)

    if ctx is not None:
        for ach in new_ach:
            txt = f"🏆 Достижение получено: {ach.name}\n{ach.description}"
            if ach.reward_gold > 0 or ach.reward_exp > 0:
                txt += f"\nНаграда: {ach.reward_gold}💰, {ach.reward_exp}✨"
            await ctx.bot.send_message(chat_id=uid, text=txt)
    return len(new_ach)

async def send_achievements(uid: int, ctx: ContextTypes.DEFAULT_TYPE):
    await get_player(uid, None)
    stats = await get_player_stats(uid)
    inv = await get_inventory(uid)
    inv_total = sum(inv.values())
    tools = await get_player_tools(uid)
    unlocked, daily_completed, weekly_completed = await get_achievements_data(uid)

    data = {
        'stats': stats,
        'inv_total': inv_total,
        'inv': inv,
        'tools': tools,
        'daily_completed': daily_completed,
        'weekly_completed': weekly_completed
    }

    text = "🏆 **Ваши достижения**\n\n"
    for ach in ACHIEVEMENTS:
        if ach.id in unlocked:
            text += f"✅ **{ach.name}**\n   {ach.description}\n"
            if ach.reward_gold > 0 or ach.reward_exp > 0:
                text += f"   🎁 Награда: {ach.reward_gold}💰, {ach.reward_exp}✨\n"
            text += "\n"
        else:
            achieved, prog, maxp = evaluate_achievement(ach, uid, data)
            percent = int(prog / maxp * 100) if maxp else 0
            bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
            text += f"🔜 **{ach.name}**\n   {ach.description}\n   Прогресс: {prog}/{maxp} {bar}\n"
            if ach.reward_gold > 0 or ach.reward_exp > 0:
                text += f"   🎁 Награда: {ach.reward_gold}💰, {ach.reward_exp}✨\n"
            text += "\n"
    await ctx.bot.send_message(chat_id=uid, text=text, parse_mode='Markdown')

# ==================== КРАФТ (РЕЦЕПТЫ) ====================

CRAFT_RECIPES = {
    # Категория: зелья
    'speed_potion': {
        'name': '⚗️ Зелье скорости',
        'description': '➕50% к опыту на 30 минут',
        'category': 'potions',
        'resources': {'coal': 5, 'iron': 2, 'magic_essence': 1},
        'result_item_id': 'speed_potion',
        'result_type': 'consumable',
        'effect': {'exp_multiplier': 1.5},
        'duration': 1800  # в секундах
    },
    'luck_elixir': {
        'name': '🍀 Эликсир удачи',
        'description': '➕10% к шансу крита на 1 час',
        'category': 'potions',
        'resources': {'gold': 3, 'diamond': 2, 'dragon_scale': 1},
        'result_item_id': 'luck_elixir',
        'result_type': 'consumable',
        'effect': {'crit_chance_bonus': 10},
        'duration': 3600
    },
    # Категория: ключи
    'goblin_key': {
        'name': '🔑 Ключ от логова гоблинов',
        'description': 'Позволяет сразиться с Королём гоблинов ещё раз',
        'category': 'keys',
        'resources': {'coal': 50, 'iron': 20, 'gold': 5},
        'result_item_id': 'goblin_key',
        'result_type': 'key',
        'effect': {'boss_id': 'goblin_king'}
    },
    'dragon_key': {
        'name': '🔑 Ключ от логова дракона',
        'description': 'Позволяет сразиться с Огненным драконом ещё раз',
        'category': 'keys',
        'resources': {'diamond': 30, 'soul_shard': 10, 'dragon_scale': 3},
        'result_item_id': 'dragon_key',
        'result_type': 'key',
        'effect': {'boss_id': 'dragon_lair'}
    },
    'lich_key': {
        'name': '🔑 Ключ от цитадели лича',
        'description': 'Позволяет сразиться с Архиличем ещё раз',
        'category': 'keys',
        'resources': {'mithril': 20, 'magic_essence': 15, 'dragon_scale': 5},
        'result_item_id': 'lich_key',
        'result_type': 'key',
        'effect': {'boss_id': 'lich_castle'}
    },
    # Категория: модификаторы для инструментов (постоянные)
    'sharp_teeth': {
        'name': '⚔️ Острые зубья',
        'description': '➕2 к силе активного инструмента (постоянно)',
        'category': 'mods',
        'resources': {'iron': 20, 'gold': 10, 'diamond': 5},
        'result_item_id': 'sharp_teeth',
        'result_type': 'permanent',
        'effect': {'tool_power_bonus': 2}
    },
    'magic_rune': {
        'name': '🔮 Магическая руна',
        'description': '➕5% к шансу двойной добычи (постоянно)',
        'category': 'mods',
        'resources': {'mithril': 10, 'soul_shard': 3, 'dragon_scale': 2},
        'result_item_id': 'magic_rune',
        'result_type': 'permanent',
        'effect': {'crit_chance_bonus_permanent': 5}
    },
    # Категория: конвертация
    'gold_ore_craft': {
        'name': '🪙 Синтез золотой руды',
        'description': 'Преобразовать 10 угля + 5 железа в 1 золотую руду',
        'category': 'conversion',
        'resources': {'coal': 10, 'iron': 5},
        'result_item_id': 'gold',
        'result_type': 'resource',
        'effect': {'resource_id': 'gold', 'amount': 1}
    },
    'diamond_craft': {
        'name': '💎 Синтез алмаза',
        'description': 'Преобразовать 20 золота + 10 железа в 1 алмаз',
        'category': 'conversion',
        'resources': {'gold': 20, 'iron': 10},
        'result_item_id': 'diamond',
        'result_type': 'resource',
        'effect': {'resource_id': 'diamond', 'amount': 1}
    },
    'mithril_craft': {
        'name': '🔮 Синтез мифрила',
        'description': 'Преобразовать 30 алмазов + 15 золота в 1 мифрил',
        'category': 'conversion',
        'resources': {'diamond': 30, 'gold': 15},
        'result_item_id': 'mithril',
        'result_type': 'resource',
        'effect': {'resource_id': 'mithril', 'amount': 1}
    },
}

category_map = {
    'potions': '⚗️ Зелья',
    'keys': '🔑 Ключи',
    'mods': '⚔️ Модификаторы',
    'conversion': '🔄 Конвертация ресурсов'
}

# ---------- Предметы (крафт) ----------
async def get_player_items(uid: int, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        rows = await conn.fetch("SELECT item_id, quantity FROM player_items WHERE user_id = $1", uid)
        return {row['item_id']: row['quantity'] for row in rows}
    if conn:
        return await _get(conn)
    else:
        async with db_pool.acquire() as conn:
            return await _get(conn)

async def add_item(uid: int, item_id: str, quantity: int = 1, expires_at: datetime.datetime = None, conn: asyncpg.Connection = None):
    async def _add(conn):
        if expires_at:
            await conn.execute("""
                INSERT INTO player_items (user_id, item_id, quantity, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET quantity = player_items.quantity + EXCLUDED.quantity
            """, uid, item_id, quantity, expires_at)
        else:
            await conn.execute("""
                INSERT INTO player_items (user_id, item_id, quantity)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET quantity = player_items.quantity + EXCLUDED.quantity
            """, uid, item_id, quantity)
    if conn:
        await _add(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await _add(conn)

async def remove_item(uid: int, item_id: str, quantity: int = 1, conn: asyncpg.Connection = None) -> bool:
    async def _remove(conn):
        cur = await conn.fetchval("SELECT quantity FROM player_items WHERE user_id = $1 AND item_id = $2", uid, item_id)
        if not cur or cur < quantity:
            return False
        new_qty = cur - quantity
        if new_qty == 0:
            await conn.execute("DELETE FROM player_items WHERE user_id = $1 AND item_id = $2", uid, item_id)
        else:
            await conn.execute("UPDATE player_items SET quantity = $1 WHERE user_id = $2 AND item_id = $3", new_qty, uid, item_id)
        return True
    if conn:
        return await _remove(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _remove(conn)

async def craft_item(uid: int, recipe_id: str, conn: asyncpg.Connection = None) -> Tuple[bool, str]:
    recipe = CRAFT_RECIPES.get(recipe_id)
    if not recipe:
        return False, "Рецепт не найден"
    
    async def _craft(conn):
        inv = await get_inventory(uid, conn)
        for res, need in recipe['resources'].items():
            if inv.get(res, 0) < need:
                return False, f"Недостаточно {RESOURCES[res]['name']}"
        
        for res, need in recipe['resources'].items():
            await remove_resource(uid, res, need, conn)
        
        result_type = recipe.get('result_type')
        if result_type == 'resource':
            await add_resource(uid, recipe['effect']['resource_id'], recipe['effect']['amount'], conn)
            return True, f"✅ Создано: {recipe['effect']['amount']} {RESOURCES[recipe['effect']['resource_id']]['name']}"
        elif result_type in ('consumable', 'key', 'permanent'):
            if recipe.get('duration'):
                expires_at = datetime.datetime.now() + datetime.timedelta(seconds=recipe['duration'])
            else:
                expires_at = None
            await add_item(uid, recipe['result_item_id'], 1, expires_at, conn)
            return True, f"✅ Создано: {recipe['name']}"
        else:
            return False, "Неизвестный тип результата"
    
    if conn:
        async with conn.transaction():
            return await _craft(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _craft(conn)

# ==================== ЭФФЕКТЫ (БАФФЫ) ====================

async def apply_effect(uid: int, effect_id: str, effect_data: dict, duration: int, conn: asyncpg.Connection = None):
    """Добавляет временный эффект игроку."""
    expires_at = datetime.datetime.now() + datetime.timedelta(seconds=duration)
    async def _apply(conn):
        await conn.execute("""
            INSERT INTO active_effects (user_id, effect_id, expires_at, effect_data)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (user_id, effect_id) DO UPDATE
            SET expires_at = $3, effect_data = $4::jsonb
        """, uid, effect_id, expires_at, json.dumps(effect_data))
    if conn:
        await _apply(conn)
    else:
        async with db_pool.acquire() as conn:
            await _apply(conn)

async def get_active_effects(uid: int, conn: asyncpg.Connection = None) -> dict:
    """Возвращает словарь активных эффектов игрока."""
    async def _get(conn):
        rows = await conn.fetch(
            "SELECT effect_id, effect_data FROM active_effects WHERE user_id = $1 AND expires_at > NOW()",
            uid
        )
        effects = {}
        for row in rows:
            effects[row['effect_id']] = json.loads(row['effect_data'])
        return effects
    if conn:
        return await _get(conn)
    else:
        async with db_pool.acquire() as conn:
            return await _get(conn)

# ==================== ОБЩАЯ ЛОГИКА КЛИКА ====================

async def process_click(uid: int, conn: asyncpg.Connection = None) -> dict:
    """
    Выполняет логику одного клика в транзакции.
    Возвращает словарь с результатами.
    """
    async def _execute(conn):
        # ----- НАЧАЛО НОВОГО КОДА (ЭФФЕКТЫ) -----
        # Получаем активные эффекты
        effects = await get_active_effects(uid, conn)
        exp_multiplier = 1.0
        crit_bonus = 0
        for eff in effects.values():
            if 'exp_multiplier' in eff:
                exp_multiplier *= eff['exp_multiplier']
            if 'crit_chance_bonus' in eff:
                crit_bonus += eff['crit_chance_bonus']
        # ----- КОНЕЦ НОВОГО КОДА -----

        # Получаем текущую локацию
        loc_id = await get_player_current_location(uid, conn)
        loc = LOCATIONS.get(loc_id, LOCATIONS['coal_mine'])

        # Добыча ресурса
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

        # Базовая награда
        stats = await get_player_stats(uid, conn)
        gold, exp, is_crit = get_click_reward(stats)

        # ----- ПРИМЕНЯЕМ ЭФФЕКТЫ К НАГРАДЕ -----
        exp = int(exp * exp_multiplier)
        if crit_bonus:
            extra_crit = random.random() < crit_bonus / 100
            if extra_crit and not is_crit:
                is_crit = True
                gold *= 2
                exp *= 2

        # Модификатор от инструмента
        if found:
            active_tool = await get_active_tool(uid, conn)
            tool_level = await get_tool_level(uid, active_tool, conn)
            # Учитываем постоянный бонус от модификаторов
            tool_power = get_tool_power(uid, active_tool, tool_level) + stats.get('perm_tool_power_bonus', 0)
            if tool_power > 0:
                multiplier = 1 + (tool_power - 1) * 0.2
                amt = int(amt * multiplier)
                amt = max(1, amt)

        # Обновляем игрока
        await conn.execute("""
            UPDATE players
            SET gold = gold + $1,
                exp = exp + $2,
                total_clicks = total_clicks + 1,
                total_gold_earned = total_gold_earned + $3,
                total_crits = total_crits + $4,
                current_crit_streak = CASE WHEN $5 THEN current_crit_streak + 1 ELSE 0 END,
                max_crit_streak = GREATEST(max_crit_streak,
                                           CASE WHEN $5 THEN current_crit_streak + 1 ELSE max_crit_streak END)
            WHERE user_id = $6
        """, gold, exp, gold, 1 if is_crit else 0, is_crit, uid)

        # Добавляем ресурс, если найден
        if found:
            await add_resource(uid, found, amt, conn)

        # Обновляем задания
        await update_daily_task_progress(uid, 'Труженик', 1, conn)
        await update_daily_task_progress(uid, 'Золотоискатель', gold, conn)
        if is_crit:
            await update_daily_task_progress(uid, 'Везунчик', 1, conn)
        if found:
            await update_daily_task_progress(uid, 'Рудокоп', amt, conn)
            await update_daily_task_progress(uid, 'Горняк', amt, conn)

        await update_weekly_task_progress(uid, 'Шахтёр', 1, conn)
        await update_weekly_task_progress(uid, 'Золотая лихорадка', gold, conn)
        if is_crit:
            await update_weekly_task_progress(uid, 'Критический удар', 1, conn)
        if found:
            await update_weekly_task_progress(uid, 'Коллекционер', amt, conn)

        # Проверка достижений
        await check_achievements(uid, conn=conn)

        # Повышаем уровень, если нужно
        await level_up_if_needed(uid, conn)

        # Получаем свежие данные для ответа
        new_stats = await get_player_stats(uid, conn)
        new_inv = await get_inventory(uid, conn)

        return {
            'gold': gold,
            'exp': exp,
            'is_crit': is_crit,
            'found_resource': found,
            'amount': amt,
            'new_gold': new_stats['gold'],
            'new_exp': new_stats['exp'],
            'inventory': new_inv
        }

    if conn is None:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _execute(conn)
    else:
        return await _execute(conn)

# ==================== КРАФТ (РЕЦЕПТЫ) ====================

CRAFT_RECIPES = {
    # Категория: зелья
    'speed_potion': {
        'name': '⚗️ Зелье скорости',
        'description': '➕50% к опыту на 30 минут',
        'category': 'potions',
        'resources': {'coal': 5, 'iron': 2, 'magic_essence': 1},
        'result_item_id': 'speed_potion',
        'result_type': 'consumable',
        'effect': {'exp_multiplier': 1.5},
        'duration': 1800  # в секундах
    },
    'luck_elixir': {
        'name': '🍀 Эликсир удачи',
        'description': '➕10% к шансу крита на 1 час',
        'category': 'potions',
        'resources': {'gold': 3, 'diamond': 2, 'dragon_scale': 1},
        'result_item_id': 'luck_elixir',
        'result_type': 'consumable',
        'effect': {'crit_chance_bonus': 10},
        'duration': 3600
    },
    # Категория: ключи
    'goblin_key': {
        'name': '🔑 Ключ от логова гоблинов',
        'description': 'Позволяет сразиться с Королём гоблинов ещё раз',
        'category': 'keys',
        'resources': {'coal': 50, 'iron': 20, 'gold': 5},
        'result_item_id': 'goblin_key',
        'result_type': 'key',
        'effect': {'boss_id': 'goblin_king'}
    },
    'dragon_key': {
        'name': '🔑 Ключ от логова дракона',
        'description': 'Позволяет сразиться с Огненным драконом ещё раз',
        'category': 'keys',
        'resources': {'diamond': 30, 'soul_shard': 10, 'dragon_scale': 3},
        'result_item_id': 'dragon_key',
        'result_type': 'key',
        'effect': {'boss_id': 'dragon_lair'}
    },
    'lich_key': {
        'name': '🔑 Ключ от цитадели лича',
        'description': 'Позволяет сразиться с Архиличем ещё раз',
        'category': 'keys',
        'resources': {'mithril': 20, 'magic_essence': 15, 'dragon_scale': 5},
        'result_item_id': 'lich_key',
        'result_type': 'key',
        'effect': {'boss_id': 'lich_castle'}
    },
    # Категория: модификаторы для инструментов (постоянные)
    'sharp_teeth': {
        'name': '⚔️ Острые зубья',
        'description': '➕2 к силе активного инструмента (постоянно)',
        'category': 'mods',
        'resources': {'iron': 20, 'gold': 10, 'diamond': 5},
        'result_item_id': 'sharp_teeth',
        'result_type': 'permanent',
        'effect': {'tool_power_bonus': 2}
    },
    'magic_rune': {
        'name': '🔮 Магическая руна',
        'description': '➕5% к шансу двойной добычи (постоянно)',
        'category': 'mods',
        'resources': {'mithril': 10, 'soul_shard': 3, 'dragon_scale': 2},
        'result_item_id': 'magic_rune',
        'result_type': 'permanent',
        'effect': {'crit_chance_bonus_permanent': 5}
    },
    # Категория: конвертация
    'gold_ore_craft': {
        'name': '🪙 Синтез золотой руды',
        'description': 'Преобразовать 10 угля + 5 железа в 1 золотую руду',
        'category': 'conversion',
        'resources': {'coal': 10, 'iron': 5},
        'result_item_id': 'gold',
        'result_type': 'resource',
        'effect': {'resource_id': 'gold', 'amount': 1}
    },
    'diamond_craft': {
        'name': '💎 Синтез алмаза',
        'description': 'Преобразовать 20 золота + 10 железа в 1 алмаз',
        'category': 'conversion',
        'resources': {'gold': 20, 'iron': 10},
        'result_item_id': 'diamond',
        'result_type': 'resource',
        'effect': {'resource_id': 'diamond', 'amount': 1}
    },
    'mithril_craft': {
        'name': '🔮 Синтез мифрила',
        'description': 'Преобразовать 30 алмазов + 15 золота в 1 мифрил',
        'category': 'conversion',
        'resources': {'diamond': 30, 'gold': 15},
        'result_item_id': 'mithril',
        'result_type': 'resource',
        'effect': {'resource_id': 'mithril', 'amount': 1}
    },
}

category_map = {
    'potions': '⚗️ Зелья',
    'keys': '🔑 Ключи',
    'mods': '⚔️ Модификаторы',
    'conversion': '🔄 Конвертация ресурсов'
}

# ---------- Предметы (крафт) ----------
async def get_player_items(uid: int, conn: asyncpg.Connection = None) -> dict:
    async def _get(conn):
        rows = await conn.fetch("SELECT item_id, quantity FROM player_items WHERE user_id = $1", uid)
        return {row['item_id']: row['quantity'] for row in rows}
    if conn:
        return await _get(conn)
    else:
        async with db_pool.acquire() as conn:
            return await _get(conn)

async def add_item(uid: int, item_id: str, quantity: int = 1, expires_at: datetime.datetime = None, conn: asyncpg.Connection = None):
    async def _add(conn):
        if expires_at:
            await conn.execute("""
                INSERT INTO player_items (user_id, item_id, quantity, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET quantity = player_items.quantity + EXCLUDED.quantity
            """, uid, item_id, quantity, expires_at)
        else:
            await conn.execute("""
                INSERT INTO player_items (user_id, item_id, quantity)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, item_id) DO UPDATE
                SET quantity = player_items.quantity + EXCLUDED.quantity
            """, uid, item_id, quantity)
    if conn:
        await _add(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await _add(conn)

async def remove_item(uid: int, item_id: str, quantity: int = 1, conn: asyncpg.Connection = None) -> bool:
    async def _remove(conn):
        cur = await conn.fetchval("SELECT quantity FROM player_items WHERE user_id = $1 AND item_id = $2", uid, item_id)
        if not cur or cur < quantity:
            return False
        new_qty = cur - quantity
        if new_qty == 0:
            await conn.execute("DELETE FROM player_items WHERE user_id = $1 AND item_id = $2", uid, item_id)
        else:
            await conn.execute("UPDATE player_items SET quantity = $1 WHERE user_id = $2 AND item_id = $3", new_qty, uid, item_id)
        return True
    if conn:
        return await _remove(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _remove(conn)

async def craft_item(uid: int, recipe_id: str, conn: asyncpg.Connection = None) -> Tuple[bool, str]:
    recipe = CRAFT_RECIPES.get(recipe_id)
    if not recipe:
        return False, "Рецепт не найден"
    
    async def _craft(conn):
        inv = await get_inventory(uid, conn)
        for res, need in recipe['resources'].items():
            if inv.get(res, 0) < need:
                return False, f"Недостаточно {RESOURCES[res]['name']}"
        
        for res, need in recipe['resources'].items():
            await remove_resource(uid, res, need, conn)
        
        result_type = recipe.get('result_type')
        if result_type == 'resource':
            await add_resource(uid, recipe['effect']['resource_id'], recipe['effect']['amount'], conn)
            return True, f"✅ Создано: {recipe['effect']['amount']} {RESOURCES[recipe['effect']['resource_id']]['name']}"
        elif result_type in ('consumable', 'key', 'permanent'):
            if recipe.get('duration'):
                expires_at = datetime.datetime.now() + datetime.timedelta(seconds=recipe['duration'])
            else:
                expires_at = None
            await add_item(uid, recipe['result_item_id'], 1, expires_at, conn)
            return True, f"✅ Создано: {recipe['name']}"
        else:
            return False, "Неизвестный тип результата"
    
    if conn:
        async with conn.transaction():
            return await _craft(conn)
    else:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                return await _craft(conn)

# ==================== ФУНКЦИИ ОТОБРАЖЕНИЯ (КРАФТ) ====================

async def show_craft_menu(update_or_query, ctx):
    kb = [
        [InlineKeyboardButton("⚗️ Зелья", callback_data='craft_category_potions')],
        [InlineKeyboardButton("🔑 Ключи", callback_data='craft_category_keys')],
        [InlineKeyboardButton("⚔️ Модификаторы", callback_data='craft_category_mods')],
        [InlineKeyboardButton("🔄 Конвертация ресурсов", callback_data='craft_category_conversion')],
        [InlineKeyboardButton("🎒 Мои предметы", callback_data='craft_my_items')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    txt = "🔨 **Крафт**\n\nВыберите категорию рецептов:"
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_craft_category(update_or_query, ctx, category):
    uid = update_or_query.from_user.id
    inv = await get_inventory(uid)
    txt = f"🔨 **Категория: {category_map.get(category, category)}**\n\n"
    kb = []
    for rid, recipe in CRAFT_RECIPES.items():
        if recipe['category'] != category:
            continue
        name = recipe['name']
        desc = recipe['description']
        resources = []
        for res, need in recipe['resources'].items():
            have = inv.get(res, 0)
            emoji = "🟢" if have >= need else "🔴"
            res_name = RESOURCES[res]['name']
            resources.append(f"{emoji} {res_name} {need} (у вас {have})")
        res_str = "\n      ".join(resources)
        txt += f"**{name}**\n{desc}\n   Требуется:\n      {res_str}\n\n"
        kb.append([InlineKeyboardButton(f"Создать {name}", callback_data=f'craft_do_{rid}')])
    if not kb:
        txt += "В этой категории пока нет рецептов.\n"
    kb.append([InlineKeyboardButton("🔙 К категориям", callback_data='craft_menu')])
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_craft_my_items(update_or_query, ctx):
    uid = update_or_query.from_user.id
    items = await get_player_items(uid)
    if not items:
        txt = "🎒 **Мои предметы**\n\nУ вас пока нет созданных предметов."
    else:
        txt = "🎒 **Мои предметы**\n\n"
        for item_id, qty in items.items():
            # Ищем название рецепта по result_item_id
            name = next((r['name'] for r in CRAFT_RECIPES.values() if r['result_item_id'] == item_id), item_id)
            txt += f"• {name} x{qty}\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='craft_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def craft_do(update_or_query, ctx, recipe_id):
    uid = update_or_query.from_user.id
    success, msg = await craft_item(uid, recipe_id)
    if success:
        await update_or_query.answer("✅ Предмет создан!", show_alert=False)
        await ctx.bot.send_message(chat_id=uid, text=msg)
    else:
        await update_or_query.answer(msg, show_alert=True)
    # Возвращаемся в категорию
    recipe = CRAFT_RECIPES.get(recipe_id)
    if recipe:
        await show_craft_category(update_or_query, ctx, recipe['category'])
    else:
        await show_craft_menu(update_or_query, ctx)

# ==================== ОБНОВЛЁННОЕ ГЛАВНОЕ МЕНЮ ====================

async def show_main_menu(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    kb = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine'),
         InlineKeyboardButton("📋 Задания", callback_data='tasks'),
         InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard_menu')]
    ]
    kb.append([InlineKeyboardButton("🔨 Крафт", callback_data='craft_menu')])
    if stats['level'] >= 5:
        kb.append([InlineKeyboardButton("⚔️ Босс-арена (3D)", web_app=WebAppInfo(url="https://vladislavbropiton.github.io/telegram-clicker-bot/"))])
    rm = InlineKeyboardMarkup(kb)
    txt = ("🪨 **Шахтёрская глубина**\n\nПривет, шахтёр! Твой путь к богатству начинается здесь.\n\n🏁 **Что делать?**\n• Нажимай «⛏ Добыть» – каждый клик приносит золото и ресурсы.\n• Выполняй «📋 Задания» – получай бонусы.\n• Соревнуйся в «🏆 Лидеры» – стань лучшим!\n• Создавай предметы в «🔨 Крафт».\n\nОстальные команды доступны в меню (кнопка слева внизу).")
    await reply_or_edit(update_or_query, txt, reply_markup=rm, parse_mode='Markdown')

# ==================== ФУНКЦИИ ОТОБРАЖЕНИЯ ====================

async def show_main_menu(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    kb = [
        [InlineKeyboardButton("⛏ Добыть", callback_data='mine'),
         InlineKeyboardButton("📋 Задания", callback_data='tasks'),
         InlineKeyboardButton("🏆 Лидеры", callback_data='leaderboard_menu')]
    ]
    kb.append([InlineKeyboardButton("🔨 Крафт", callback_data='craft_menu')])
    if stats['level'] >= 5:
        kb.append([InlineKeyboardButton("⚔️ Босс-арена (3D)", web_app=WebAppInfo(url="https://vladislavbropiton.github.io/telegram-clicker-bot/"))])
    rm = InlineKeyboardMarkup(kb)
    txt = ("🪨 **Шахтёрская глубина**\n\nПривет, шахтёр! Твой путь к богатству начинается здесь.\n\n🏁 **Что делать?**\n• Нажимай «⛏ Добыть» – каждый клик приносит золото и ресурсы.\n• Выполняй «📋 Задания» – получай бонусы.\n• Соревнуйся в «🏆 Лидеры» – стань лучшим!\n• Создавай предметы в «🔨 Крафт».\n\nОстальные команды доступны в меню (кнопка слева внизу).")
    await reply_or_edit(update_or_query, txt, reply_markup=rm, parse_mode='Markdown')

async def show_main_menu_from_query(query, ctx=None):
    await show_main_menu(query, ctx)

async def show_locations(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    cur = await get_player_current_location(uid)
    stats = await get_player_stats(uid)
    lvl = stats['level']
    tool_level = await get_active_tool_level(uid)
    sl = sorted(LOCATIONS.items(), key=lambda x: x[1]['min_level'])
    
    txt = "🗺 **Обычные локации**\n\n"
    kb = []
    
    for lid, loc in sl:
        level_ok = lvl >= loc['min_level']
        tool_ok = tool_level >= loc.get('min_tool_level', 0) if loc.get('min_tool_level', 0) > 0 else True
        avail = level_ok and tool_ok
        is_cur = (lid == cur)
        status = "✅" if avail else "🔒"
        mark = "📍" if is_cur else ""
        loc_name = escape_markdown(loc['name'], version=1)
        
        line = f"{mark}{status} **{loc_name}**"
        if not level_ok:
            line += f" (треб. ур.{loc['min_level']})"
        elif not tool_ok:
            line += f" (треб. инстр. {loc['min_tool_level']} ур.)"
        else:
            line += f" (доступна)"
        txt += line + "\n   " + loc['description'] + "\n"
        
        # Добавляем кнопку перехода, если локация доступна и не текущая
        if avail and not is_cur:
            kb.append([InlineKeyboardButton(f"Перейти в {loc['name']}", callback_data=f'goto_{lid}')])
    
    # Босс-локации (информационно, без кнопок перехода)
    txt += "\n⚔️ **Босс-локации**\n\n"
    for bid, bloc in BOSS_LOCATIONS.items():
        level_ok = lvl >= bloc['min_level']
        tool_ok = tool_level >= bloc['min_tool_level']
        if level_ok and tool_ok:
            prog = await get_boss_progress(uid, bid)
            status = "✅" if prog['defeated'] else "⚔️"
            txt += f"{status} **{bloc['name']}**\n   {bloc['description']}\n"
            if not prog['defeated']:
                txt += f"   Здоровье: {prog['current_health']}/{bloc['boss']['health']}\n"
            else:
                txt += "   (побеждён)\n"
        else:
            txt += f"🔒 **{bloc['name']}** (треб. ур.{bloc['min_level']}, инстр.{bloc['min_tool_level']})\n"
        txt += "\n"
    
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_shop_menu(update_or_query, ctx):
    kb = [[InlineKeyboardButton("⚡ Улучшения", callback_data='shop_category_upgrades'),
            InlineKeyboardButton("🧰 Инструменты", callback_data='shop_category_tools')],
           [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    txt = "🛒 **Магазин**\n\nЗдесь ты можешь улучшить своего шахтёра. Выбери категорию:\n\n⚡ Улучшения – прокачка навыков\n🧰 Инструменты – покупка и улучшение кирок"
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_shop_upgrades(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    gold = stats['gold']
    txt = f"⚡ **Улучшения**\n💰 Твой баланс: {gold} золота\n\n"
    kb = []
    for up_id, info in UPGRADES.items():
        lvl = stats['upgrades'][up_id]
        price = int(info['base_price'] * (info['price_mult'] ** lvl))
        name = escape_markdown(info['name'], version=1)
        txt += f"─────────────────────────\n**{name}** (ур.{lvl})\n   {info['description']}\n   💰 Следующий уровень: {price}\n\n"
        kb.append([InlineKeyboardButton(f"Купить {info['name']} за {price}", callback_data=f'buy_{up_id}')])
    txt += "─────────────────────────\nЧтобы купить, нажми на кнопку ниже."
    kb.append([InlineKeyboardButton("🔙 В меню магазина", callback_data='back_to_shop_menu')])
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_shop_tools(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    gold = stats['gold']
    active = await get_active_tool(uid)
    txt = f"🧰 **Инструменты**\n💰 Твой баланс: {gold} золота\n\n"
    kb = []
    for tid, tool in TOOLS.items():
        level = await get_tool_level(uid, tid)
        tool_name = escape_markdown(tool['name'], version=1)
        if level == 0 and tool['price'] > 0:
            txt += f"─────────────────────────\n🔒 **{tool_name}** – {tool['price']}💰 (треб.ур.{tool['required_level']})\n   {tool['description']}\n\n"
            kb.append([InlineKeyboardButton(f"Купить {tool['name']} за {tool['price']}", callback_data=f'buy_tool_{tid}')])
        elif level > 0:
            is_active = (tid == active)
            active_mark = "📍" if is_active else ""
            power = get_tool_power(uid, tid, level)
            txt += f"─────────────────────────\n{active_mark} **{tool_name}** ур.{level} (сила {power})\n   {tool['description']}\n"
            row = []
            if not is_active:
                row.append(InlineKeyboardButton("🔨 Сделать активным", callback_data=f'activate_tool_{tid}'))
            if await can_upgrade_tool(uid, tid):
                cost = get_upgrade_cost(tid, level)
                cost_parts = [f"{escape_markdown(RESOURCES[res]['name'], version=1)} {amt}" for res, amt in cost.items()]
                cost_str = ", ".join(cost_parts)
                row.append(InlineKeyboardButton(f"⬆️ Улучшить ({cost_str})", callback_data=f'upgrade_tool_{tid}'))
            if row:
                kb.append(row)
    txt += "\n─────────────────────────\nВыбери действие."
    kb.append([InlineKeyboardButton("🔙 В меню магазина", callback_data='back_to_shop_menu')])
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_daily_tasks(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    await check_daily_reset(uid)
    daily = await get_daily_tasks(uid)
    txt = "📋 **Ежедневные задания**\n\n"
    if daily:
        for t in daily:
            _, n, desc, g, prog, com, rg, re = t
            if com:
                st = "✅"
            else:
                percent = int(prog / g * 100) if g > 0 else 0
                bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
                st = f"{prog}/{g} {bar}"
            name = escape_markdown(n, version=1)
            desc_esc = escape_markdown(desc, version=1)
            txt += f"🔹 {name}: {desc_esc}\n   Прогресс: {st}\n   Награда: {rg}💰 + {re}✨\n\n"
    else:
        txt += "Нет заданий на сегодня.\n\n"
    kb = [
        [InlineKeyboardButton("📅 Еженедельные", callback_data='show_weekly')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_weekly_tasks(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    await check_weekly_reset(uid)
    weekly = await get_weekly_tasks(uid)
    txt = "📅 **Еженедельные задания**\n\n"
    if weekly:
        for t in weekly:
            _, n, desc, g, prog, com, rg, re = t
            if com:
                st = "✅"
            else:
                percent = int(prog / g * 100) if g > 0 else 0
                bar = "█" * (percent // 10) + "░" * (10 - (percent // 10))
                st = f"{prog}/{g} {bar}"
            name = escape_markdown(n, version=1)
            desc_esc = escape_markdown(desc, version=1)
            txt += f"🔸 {name}: {desc_esc}\n   Прогресс: {st}\n   Награда: {rg}💰 + {re}✨\n\n"
    else:
        txt += "Нет заданий на эту неделю.\n\n"
    kb = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_daily')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_profile(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    if not stats:
        await reply_or_edit(update_or_query, "Профиль не найден.")
        return
    username = escape_markdown(update_or_query.from_user.username or 'Аноним', version=1) if hasattr(update_or_query, 'from_user') else 'Аноним'
    txt = (f"👤 **Профиль игрока**\n\n📊 **Статистика**\n• Уровень: **{stats['level']}**\n"
           f"• Общий опыт: **{stats['total_exp']}**\n"
           f"• Золото: **{stats['gold']}**💰\n• Всего кликов: **{stats['clicks']}**\n"
           f"• Всего добыто золота: **{stats['total_gold']}**💰\n• Критические удары: **{stats['total_crits']}**\n"
           f"• Макс. серия критов: **{stats['max_crit_streak']}**\n\n⚡ **Улучшения**\n"
           f"• Сила клика: ур.**{stats['upgrades']['click_power']}**\n• Шанс крита: ур.**{stats['upgrades']['crit_chance']}**\n")
    async with db_pool.acquire() as conn:
        recent = await conn.fetch("SELECT achievement_id, unlocked_at FROM user_achievements WHERE user_id = $1 ORDER BY unlocked_at DESC LIMIT 5", uid)
    if recent:
        txt += f"\n🏅 **Последние достижения**\n"
        for aid, dt in recent:
            ach = next((a for a in ACHIEVEMENTS if a.id == aid), None)
            if ach:
                ach_name = escape_markdown(ach.name, version=1)
                txt += f"• {ach_name} ({dt})\n"
    else:
        txt += "\n🏅 **Последние достижения**\n• Пока нет\n"
    tools = await get_player_tools(uid)
    if tools:
        txt += f"\n🧰 **Инструменты**\n"
        for tid, lvl in tools.items():
            tool = TOOLS.get(tid)
            if tool:
                tool_name = escape_markdown(tool['name'], version=1)
                txt += f"• {tool_name} ур.{lvl}\n"
    kb = [[InlineKeyboardButton("🏆 Достижения", callback_data='profile_achievements'),
            InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def profile_achievements_handler(query, ctx):
    uid = query.from_user.id
    await send_achievements(uid, ctx)

async def show_inventory(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    inv = await get_inventory(uid)
    txt = "🎒 **Инвентарь**\n\nВот что ты накопал:\n\n"
    has = False
    for rid, info in RESOURCES.items():
        amt = inv.get(rid, 0)
        emoji = "🪨" if rid == 'coal' else "⚙️" if rid == 'iron' else "🟡" if rid == 'gold' else "💎" if rid == 'diamond' else "🔮"
        name = escape_markdown(info['name'], version=1)
        txt += f"{emoji} {name}: **{amt}** шт.\n"
        if amt > 0:
            has = True
    if not has:
        txt = "🎒 **Инвентарь**\n\nТвой инвентарь пока пуст. Иди добывай!\n\n"
    txt += "\n─────────────────────────\nПродать ресурсы можно на рынке (/market)."
    kb = [[InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]]
    await reply_or_edit(update_or_query, txt, reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')

async def show_market(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    inv = await get_inventory(uid)
    txt = "💰 **Рынок ресурсов**\n\nТвои запасы и текущие цены:\n\n"
    kb = []
    for rid, info in RESOURCES.items():
        amt = inv.get(rid, 0)
        price = info['base_price']
        emoji = "🪨" if rid == 'coal' else "⚙️" if rid == 'iron' else "🟡" if rid == 'gold' else "💎" if rid == 'diamond' else "🔮"
        name = escape_markdown(info['name'], version=1)
        txt += f"{emoji} {name}: **{amt}** шт. | 💰 Цена: {price} за шт.\n"
        if amt > 0:
            kb.append([InlineKeyboardButton(f"Продать 1 {name}", callback_data=f'sell_confirm_{rid}_1'),
                       InlineKeyboardButton(f"Продать всё", callback_data=f'sell_confirm_{rid}_all')])
    txt += "\n─────────────────────────\nВыбери, что и сколько продать."
    kb.append([InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')])
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_menu(update_or_query, ctx):
    kb = [
        [InlineKeyboardButton("📊 По уровню", callback_data='leaderboard_level')],
        [InlineKeyboardButton("💰 По золоту", callback_data='leaderboard_gold')],
        [InlineKeyboardButton("🏆 По достижениям", callback_data='leaderboard_achievements')],
        [InlineKeyboardButton("📅 По заданиям", callback_data='leaderboard_tasks_completed')],
        [InlineKeyboardButton("🔨 По инструментам", callback_data='leaderboard_tools')],
        [InlineKeyboardButton("📦 По ресурсам", callback_data='leaderboard_resources_menu')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_menu')]
    ]
    txt = ("🏆 **Таблица лидеров**\n\nВыбери категорию для просмотра топ-10 игроков:")
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_resources_menu(update_or_query, ctx):
    kb = [
        [InlineKeyboardButton("🪨 По углю", callback_data='leaderboard_coal')],
        [InlineKeyboardButton("⚙️ По железу", callback_data='leaderboard_iron')],
        [InlineKeyboardButton("🟡 По золотой руде", callback_data='leaderboard_gold_ore')],
        [InlineKeyboardButton("💎 По алмазам", callback_data='leaderboard_diamond')],
        [InlineKeyboardButton("🔮 По мифрилу", callback_data='leaderboard_mithril')],
        [InlineKeyboardButton("📦 По общему количеству", callback_data='leaderboard_total_resources')],
        [InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]
    ]
    txt = ("📦 **Лидеры по ресурсам**\n\nВыбери конкретный ресурс или общее количество:")
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_level(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, level, exp FROM players ORDER BY level DESC, exp DESC LIMIT 10")
    txt = "📊 **Топ по уровню**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            total_exp = (row['level'] - 1) * EXP_PER_LEVEL + row['exp']
            txt += f"{i}. {name} — уровень {row['level']} (общий опыт {total_exp})\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_gold(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, gold FROM players ORDER BY gold DESC LIMIT 10")
    txt = "💰 **Топ по золоту**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            txt += f"{i}. {name} — {row['gold']}💰\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_achievements(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT p.username, COUNT(ua.achievement_id) as cnt FROM players p LEFT JOIN user_achievements ua ON p.user_id = ua.user_id GROUP BY p.user_id ORDER BY cnt DESC LIMIT 10")
    txt = "🏆 **Топ по достижениям**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            txt += f"{i}. {name} — {row['cnt']} достижений\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_tasks_completed(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        daily = dict(await conn.fetch("SELECT user_id, COUNT(*) as cnt FROM daily_tasks WHERE completed = TRUE GROUP BY user_id"))
        weekly = dict(await conn.fetch("SELECT user_id, COUNT(*) as cnt FROM weekly_tasks WHERE completed = TRUE GROUP BY user_id"))
        all_users = set(daily.keys()) | set(weekly.keys())
        totals = []
        for uid in all_users:
            total = daily.get(uid, 0) + weekly.get(uid, 0)
            name = await conn.fetchval("SELECT username FROM players WHERE user_id = $1", uid)
            if name:
                totals.append((name, total))
    totals.sort(key=lambda x: x[1], reverse=True)
    top = totals[:10]
    txt = "📅 **Топ по выполненным заданиям**\n\n"
    if not top:
        txt += "Пока нет данных."
    else:
        for i, (name, cnt) in enumerate(top, 1):
            name = escape_markdown(name or 'Аноним', version=1)
            txt += f"{i}. {name} — {cnt} заданий\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_tools(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT p.username, SUM(pt.level) as total FROM players p LEFT JOIN player_tools pt ON p.user_id = pt.user_id GROUP BY p.user_id ORDER BY total DESC LIMIT 10")
    txt = "🔨 **Топ по уровню инструментов**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            txt += f"{i}. {name} — суммарный уровень {row['total']}\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_resource(update_or_query, ctx, rid, rname):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT p.username, i.amount FROM inventory i JOIN players p ON i.user_id = p.user_id WHERE i.resource_id = $1 ORDER BY i.amount DESC LIMIT 10", rid)
    rname_esc = escape_markdown(rname, version=1)
    txt = f"🏆 **Топ по {rname_esc}**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            txt += f"{i}. {name} — {row['amount']} шт.\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_leaderboard_coal(update_or_query, ctx): await show_leaderboard_resource(update_or_query, ctx, 'coal', 'Уголь')
async def show_leaderboard_iron(update_or_query, ctx): await show_leaderboard_resource(update_or_query, ctx, 'iron', 'Железо')
async def show_leaderboard_gold_ore(update_or_query, ctx): await show_leaderboard_resource(update_or_query, ctx, 'gold', 'Золотая руда')
async def show_leaderboard_diamond(update_or_query, ctx): await show_leaderboard_resource(update_or_query, ctx, 'diamond', 'Алмазы')
async def show_leaderboard_mithril(update_or_query, ctx): await show_leaderboard_resource(update_or_query, ctx, 'mithril', 'Мифрил')
async def show_leaderboard_total_resources(update_or_query, ctx):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT p.username, SUM(i.amount) as total FROM players p LEFT JOIN inventory i ON p.user_id = i.user_id GROUP BY p.user_id ORDER BY total DESC LIMIT 10")
    txt = "📦 **Топ по общему количеству ресурсов**\n\n"
    if not rows:
        txt += "Пока нет данных."
    else:
        for i, row in enumerate(rows, 1):
            name = escape_markdown(row['username'] or 'Аноним', version=1)
            txt += f"{i}. {name} — {row['total']} шт.\n"
    kb = [[InlineKeyboardButton("🔙 К категориям", callback_data='leaderboard_menu')]]
    await reply_or_edit(update_or_query, txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_faq_locations(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    lvl = stats['level']
    
    text = "🗺 **Локации**\n\n"
    text += "**Обычные локации:**\n\n"
    
    for loc_id, loc in LOCATIONS.items():
        emoji = "🪨" if 'coal' in loc_id else "⚙️" if 'iron' in loc_id else "🟡" if 'gold' in loc_id else "💎" if 'diamond' in loc_id else "🔮"
        name = loc['name']
        req_level = loc['min_level']
        req_tool = loc.get('min_tool_level', 0)
        tool_text = f", инструмент {req_tool} ур." if req_tool > 0 else ""
        text += f"{emoji} **{name}**\n"
        text += f"   Требуется: уровень {req_level}{tool_text}\n"
        text += f"   {loc['description']}\n"
        res_list = []
        for res in loc['resources']:
            res_name = RESOURCES[res['res_id']]['name']
            prob = int(res['prob'] * 100)
            amount = f"{res['min']}-{res['max']}" if res['min'] != res['max'] else str(res['min'])
            res_list.append(f"{res_name} {prob}% ({amount} шт.)")
        text += "   Ресурсы: " + ", ".join(res_list) + "\n\n"
    
    kb = [
        [InlineKeyboardButton("⚔️ Босс-локации", callback_data='faq_boss_locations')],
        [InlineKeyboardButton("🔙 Назад", callback_data='back_to_faq')]
    ]
    await reply_or_edit(update_or_query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def show_faq_boss_locations(update_or_query, ctx):
    text = "⚔️ **Босс-локации** ⚔️\n\n"
    for bid, bloc in BOSS_LOCATIONS.items():
        boss = bloc['boss']
        if 'goblin' in bid:
            emoji = "👑"
        elif 'dragon' in bid:
            emoji = "🐉"
        else:
            emoji = "💀"
        
        text += f"{emoji} **{bloc['name']}**\n"
        text += f"   Требуется: уровень {bloc['min_level']}, инструмент {bloc['min_tool_level']} ур.\n"
        text += f"   {bloc['description']}\n"
        text += f"   Босс: {boss['name']} | Здоровье: {boss['health']}\n"
        rewards = []
        if boss['reward_gold']:
            rewards.append(f"{boss['reward_gold']}💰")
        if boss['exp_reward']:
            rewards.append(f"{boss['exp_reward']}✨")
        for res, (minr, maxr) in boss['reward_resources'].items():
            res_name = RESOURCES.get(res, {}).get('name', res)
            amount = f"{minr}-{maxr}" if minr != maxr else str(minr)
            rewards.append(f"{res_name} {amount} шт.")
        text += f"   Награда: {', '.join(rewards)}\n\n"
    
    kb = [[InlineKeyboardButton("🔙 Назад к локациям", callback_data='faq_locations')]]
    await reply_or_edit(update_or_query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def back_to_faq(update_or_query, ctx):
    uid = update_or_query.from_user.id if not isinstance(update_or_query, Update) else update_or_query.effective_user.id
    stats = await get_player_stats(uid)
    lvl = stats['level']
    faq_dict = {item["question"]: item["answer"] for item in FAQ}
    categories = {
        "🪨 **Основное**": [
            "🪨 Как добывать ресурсы?",
            "🧰 Зачем нужны инструменты?",
            "⚡ Как увеличить доход за клик?"
        ],
        "🗺 **Локации**": [
            "🗺 Как открыть новые локации?",
            "🗺 Какие локации существуют и что там добывают?"
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
        ],
        "🔨 **Крафт**": [
            "🔨 Что такое крафт?"
        ]
    }
    text = "📚 **Часто задаваемые вопросы**\n\n"
    for category, questions in categories.items():
        text += f"{category}\n" + "─" * 25 + "\n\n"
        for q in questions:
            if q in faq_dict:
                q_esc = escape_markdown(q, version=1)
                a_esc = escape_markdown(faq_dict[q], version=1)
                text += f"❓ **{q_esc}**\n{a_esc}\n\n"
        text += "\n"
    kb = [[InlineKeyboardButton("🗺 Локации", callback_data='faq_locations')]]
    await reply_or_edit(update_or_query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

# ==================== ДЕЙСТВИЯ ====================

async def mine_action(update_or_query, ctx):
    uid = update_or_query.from_user.id if isinstance(update_or_query, Update) else update_or_query.from_user.id
    result = await process_click(uid)
    ct = "💥 КРИТ!" if result['is_crit'] else ""
    res_txt = f"\nТы нашёл: {RESOURCES[result['found_resource']]['name']} x{result['amount']}!" if result['found_resource'] else ""
    txt = f"Ты добыл: {result['gold']} золота {ct}{res_txt}\nПолучено опыта: {result['exp']}"
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text(txt)
        await show_main_menu(update_or_query, ctx)
    else:
        await update_or_query.message.reply_text(txt)
        await show_main_menu_from_query(update_or_query)

async def process_buy(update_or_query, ctx):
    data = update_or_query.data
    if data.startswith('buy_tool_'):
        tid = data.replace('buy_tool_', '')
        uid = update_or_query.from_user.id
        tool = TOOLS.get(tid)
        if not tool:
            await update_or_query.answer("Ошибка!", show_alert=True)
            return
        stats = await get_player_stats(uid)
        if stats['level'] < tool['required_level']:
            await update_or_query.answer(f"❌ Требуется уровень {tool['required_level']}", show_alert=True)
            return
        if stats['gold'] < tool['price']:
            await update_or_query.answer("❌ Недостаточно золота!", show_alert=True)
            return
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE players SET gold = gold - $1 WHERE user_id = $2", tool['price'], uid)
                await conn.execute("INSERT INTO player_tools (user_id, tool_id, level, experience) VALUES ($1, $2, 1, 0) ON CONFLICT DO NOTHING", uid, tid)
        await ctx.bot.send_message(chat_id=uid, text=f"✅ Ты купил {tool['name']}!")
        await show_shop_tools(update_or_query, ctx)
        return

    up_id = data.replace('buy_', '')
    uid = update_or_query.from_user.id
    success, message, new_level = await purchase_upgrade(uid, up_id)
    if success:
        await ctx.bot.send_message(chat_id=uid, text=message)
        price = int(UPGRADES[up_id]['base_price'] * (UPGRADES[up_id]['price_mult'] ** (new_level-1)))
        await update_daily_task_progress(uid, 'Покупатель', price)
        await update_weekly_task_progress(uid, 'Магнат', price)
        await check_achievements(uid, ctx)
    else:
        await update_or_query.answer(message, show_alert=True)
    await show_shop_upgrades(update_or_query, ctx)

async def activate_tool(update_or_query, ctx):
    tid = update_or_query.data.replace('activate_tool_', '')
    uid = update_or_query.from_user.id
    await set_active_tool(uid, tid)
    await update_or_query.answer(f"✅ {TOOLS[tid]['name']} теперь активна!")
    await show_shop_tools(update_or_query, ctx)

async def upgrade_tool_handler(update_or_query, ctx):
    tid = update_or_query.data.replace('upgrade_tool_', '')
    uid = update_or_query.from_user.id
    if not await can_upgrade_tool(uid, tid):
        await update_or_query.answer("❌ Недостаточно ресурсов!", show_alert=True)
        await show_shop_tools(update_or_query, ctx)
        return
    level = await get_tool_level(uid, tid)
    cost = get_upgrade_cost(tid, level)
    cost_text = "\n".join([f"{escape_markdown(RESOURCES[res]['name'], version=1)}: {amt}" for res, amt in cost.items()])
    kb = [[InlineKeyboardButton("✅ Подтвердить", callback_data=f'confirm_upgrade_{tid}'),
            InlineKeyboardButton("❌ Отмена", callback_data='back_to_shop_tools')]]
    await reply_or_edit(update_or_query,
                        f"⬆️ Улучшение {escape_markdown(TOOLS[tid]['name'], version=1)} до ур.{level+1}\n\nПотребуется:\n{cost_text}\n\nПодтверждаешь?",
                        parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def confirm_upgrade(update_or_query, ctx):
    tid = update_or_query.data.replace('confirm_upgrade_', '')
    uid = update_or_query.from_user.id
    if not await can_upgrade_tool(uid, tid):
        await update_or_query.answer("❌ Недостаточно ресурсов!", show_alert=True)
        await show_shop_tools(update_or_query, ctx)
        return
    if await upgrade_tool(uid, tid):
        new_level = await get_tool_level(uid, tid)
        await update_or_query.answer("✅ Уровень повышен!")
        await ctx.bot.send_message(chat_id=uid, text=f"🔨 {TOOLS[tid]['name']} улучшена до уровня {new_level}!")
        await check_achievements(uid, ctx)
    else:
        await update_or_query.answer("❌ Ошибка при улучшении", show_alert=True)
    await show_shop_tools(update_or_query, ctx)

async def show_sell_confirmation(update_or_query, ctx):
    data = update_or_query.data
    parts = data.split('_')
    if len(parts) < 4:
        await update_or_query.answer("Неверные данные", show_alert=True)
        return
    rid = parts[2]
    sell_type = parts[3]
    uid = update_or_query.from_user.id
    inv = await get_inventory(uid)
    avail = inv.get(rid, 0)
    if avail == 0:
        await update_or_query.answer("❌ У вас нет этого ресурса!", show_alert=True)
        await show_market(update_or_query, ctx)
        return
    qty = avail if sell_type == 'all' else 1
    price = RESOURCES[rid]['base_price']
    total = qty * price
    resource_name = RESOURCES[rid]['name']
    text = (f"⚠️ **Подтверждение продажи**\n\n"
            f"Товар: {resource_name}\n"
            f"Количество: {qty} шт.\n"
            f"Цена за шт.: {price}💰\n"
            f"Итого: {total}💰\n\n"
            f"Подтверждаете?")
    kb = [
        [InlineKeyboardButton("✅ Да, продать", callback_data=f'sell_execute_{rid}_{sell_type}')],
        [InlineKeyboardButton("❌ Нет, вернуться", callback_data='market')]
    ]
    await reply_or_edit(update_or_query, text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def process_sell_execute(update_or_query, ctx):
    data = update_or_query.data
    parts = data.split('_')
    if len(parts) < 4:
        await update_or_query.answer("Неверные данные", show_alert=True)
        return
    rid = parts[2]
    sell_type = parts[3]
    uid = update_or_query.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            avail = await conn.fetchval("SELECT amount FROM inventory WHERE user_id = $1 AND resource_id = $2", uid, rid)
            if avail is None or avail == 0:
                await update_or_query.answer("❌ Ресурс закончился!", show_alert=True)
                await show_market(update_or_query, ctx)
                return
            qty = avail if sell_type == 'all' else 1
            if qty > avail:
                await update_or_query.answer("❌ Количество изменилось. Попробуйте снова.", show_alert=True)
                await show_market(update_or_query, ctx)
                return
            price = RESOURCES[rid]['base_price']
            total = qty * price
            await conn.execute("UPDATE inventory SET amount = amount - $1 WHERE user_id = $2 AND resource_id = $3", qty, uid, rid)
            await conn.execute("UPDATE players SET gold = gold + $1 WHERE user_id = $2", total, uid)
    await update_daily_task_progress(uid, 'Продавец', total)
    await update_weekly_task_progress(uid, 'Торговец', total)
    await update_or_query.answer(f"✅ Продано {qty} {RESOURCES[rid]['name']} за {total}💰", show_alert=False)
    await show_market(update_or_query, ctx)

async def goto_location(update_or_query, ctx):
    lid = update_or_query.data.replace('goto_', '')
    uid = update_or_query.from_user.id
    loc = LOCATIONS.get(lid)
    if not loc:
        await update_or_query.answer("Локация не найдена", show_alert=True)
        return
    stats = await get_player_stats(uid)
    if stats['level'] < loc['min_level']:
        await update_or_query.answer(f"❌ Требуется уровень {loc['min_level']}", show_alert=True)
        return
    if loc.get('min_tool_level', 0) > 0:
        tool_level = await get_active_tool_level(uid)
        if tool_level < loc['min_tool_level']:
            await update_or_query.answer(f"❌ Требуется инструмент {loc['min_tool_level']} уровня", show_alert=True)
            return
    await set_player_location(uid, lid)
    await update_or_query.answer(f"✅ Ты переместился в {loc['name']}")
    await show_locations(update_or_query, ctx)

async def fight_boss(update_or_query, ctx):
    q = update_or_query
    uid = q.from_user.id
    bid = q.data.replace('fight_boss_', '')
    bloc = BOSS_LOCATIONS.get(bid)
    if not bloc:
        await q.answer("Босс не найден", show_alert=True)
        return
    
    stats = await get_player_stats(uid)
    if stats['level'] < bloc['min_level']:
        await q.answer(f"❌ Требуется уровень {bloc['min_level']}", show_alert=True)
        return
    tool_level = await get_active_tool_level(uid)
    if tool_level < bloc['min_tool_level']:
        await q.answer(f"❌ Требуется инструмент {bloc['min_tool_level']} уровня", show_alert=True)
        return
    
    progress = await get_boss_progress(uid, bid)
    if progress['defeated']:
        await q.answer("Босс уже побеждён!", show_alert=True)
        return
    
    gold, exp, is_crit = get_click_reward(stats)
    damage = gold
    if is_crit:
        damage *= 2
        crit_text = " КРИТ!"
    else:
        crit_text = ""
    
    defeated = await update_boss_health(uid, bid, damage)
    
    if defeated:
        boss = bloc['boss']
        await update_player(uid, gold=stats['gold'] + boss['reward_gold'], exp=stats['exp'] + boss['exp_reward'])
        for res, (minr, maxr) in boss['reward_resources'].items():
            amt = random.randint(minr, maxr)
            await add_resource(uid, res, amt)
        await q.message.reply_text(
            f"⚔️ Ты нанёс {damage} урона{crit_text} и ПОБЕДИЛ {boss['name']}!\n"
            f"Награда: {boss['reward_gold']}💰, {boss['exp_reward']}✨ и ресурсы!"
        )
        await check_achievements(uid, ctx)
    else:
        new_progress = await get_boss_progress(uid, bid)
        await q.message.reply_text(
            f"⚔️ Ты нанёс {damage} урона{crit_text} боссу {bloc['boss']['name']}. "
            f"Осталось здоровья: {new_progress['current_health']}/{bloc['boss']['health']}"
        )
    
    await show_locations(q, ctx)

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_main_menu(update, ctx)

async def cmd_mine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await mine_action(update, ctx)

async def cmd_locations(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_locations(update, ctx)

async def cmd_shop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_shop_menu(update, ctx)

async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_daily_tasks(update, ctx)

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_profile(update, ctx)

async def cmd_inventory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_inventory(update, ctx)

async def cmd_market(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_market(update, ctx)

async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await get_player(u.id, u.username)
    await show_leaderboard_menu(update, ctx)

async def cmd_faq(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    stats = await get_player_stats(uid)
    lvl = stats['level']
    faq_dict = {item["question"]: item["answer"] for item in FAQ}
    categories = {
        "🪨 **Основное**": [
            "🪨 Как добывать ресурсы?",
            "🧰 Зачем нужны инструменты?",
            "⚡ Как увеличить доход за клик?"
        ],
        "🗺 **Локации**": [
            "🗺 Как открыть новые локации?",
            "🗺 Какие локации существуют и что там добывают?"
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
        ],
        "🔨 **Крафт**": [
            "🔨 Что такое крафт?"
        ]
    }
    text = "📚 **Часто задаваемые вопросы**\n\n"
    for category, questions in categories.items():
        text += f"{category}\n" + "─" * 25 + "\n\n"
        for q in questions:
            if q in faq_dict:
                q_esc = escape_markdown(q, version=1)
                a_esc = escape_markdown(faq_dict[q], version=1)
                text += f"❓ **{q_esc}**\n{a_esc}\n\n"
        text += "\n"
    kb = [[InlineKeyboardButton("🗺 Локации", callback_data='faq_locations')]]
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

async def cmd_achievements(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await send_achievements(uid, ctx)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = ("🪨 **Шахтёрский бот**\n\nТы начинающий шахтёр. Кликай, добывай ресурсы, продавай их, улучшай инструменты и открывай новые локации.\n\n**Команды:**\n/start - главное меню\n/mine - копнуть в текущей локации\n/locations - выбрать локацию\n/shop - магазин улучшений\n/tasks - задания\n/profile - твой профиль\n/inventory - ресурсы\n/market - продать ресурсы\n/leaderboard - топ игроков\n/achievements - мои достижения\n/faq - часто задаваемые вопросы\n/help - это сообщение")
    await update.message.reply_text(txt, parse_mode='Markdown')

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"🔑 Ваш Telegram ID: `{uid}`", parse_mode='Markdown')

# ==================== ДИСПЕТЧЕР CALLBACK'ОВ ====================

SIMPLE_CALLBACK_HANDLERS = {
    'mine': mine_action,
    'locations': show_locations,
    'shop': show_shop_menu,
    'shop_category_upgrades': show_shop_upgrades,
    'shop_category_tools': show_shop_tools,
    'back_to_shop_menu': show_shop_menu,
    'back_to_shop_tools': show_shop_tools,
    'tasks': show_daily_tasks,
    'show_weekly': show_weekly_tasks,
    'back_to_daily': show_daily_tasks,
    'profile': show_profile,
    'profile_achievements': profile_achievements_handler,
    'leaderboard_menu': show_leaderboard_menu,
    'leaderboard_resources_menu': show_leaderboard_resources_menu,
    'leaderboard_level': show_leaderboard_level,
    'leaderboard_gold': show_leaderboard_gold,
    'leaderboard_achievements': show_leaderboard_achievements,
    'leaderboard_tasks_completed': show_leaderboard_tasks_completed,
    'leaderboard_tools': show_leaderboard_tools,
    'leaderboard_coal': show_leaderboard_coal,
    'leaderboard_iron': show_leaderboard_iron,
    'leaderboard_gold_ore': show_leaderboard_gold_ore,
    'leaderboard_diamond': show_leaderboard_diamond,
    'leaderboard_mithril': show_leaderboard_mithril,
    'leaderboard_total_resources': show_leaderboard_total_resources,
    'faq_locations': show_faq_locations,
    'faq_boss_locations': show_faq_boss_locations,
    'back_to_faq': back_to_faq,
    'inventory': show_inventory,
    'market': show_market,
    'back_to_menu': show_main_menu_from_query,
    'craft_menu': show_craft_menu,
    'craft_my_items': show_craft_my_items,
}

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    uid = q.from_user.id

    if data in SIMPLE_CALLBACK_HANDLERS:
        await SIMPLE_CALLBACK_HANDLERS[data](q, ctx)
        await q.answer()
        return

    if data.startswith('craft_category_'):
        cat = data.replace('craft_category_', '')
        await show_craft_category(q, ctx, cat)
    elif data.startswith('craft_do_'):
        recipe_id = data.replace('craft_do_', '')
        await craft_do(q, ctx, recipe_id)
    elif data.startswith('activate_tool_'):
        await activate_tool(q, ctx)
    elif data.startswith('upgrade_tool_'):
        await upgrade_tool_handler(q, ctx)
    elif data.startswith('confirm_upgrade_'):
        await confirm_upgrade(q, ctx)
    elif data.startswith('buy_'):
        await process_buy(q, ctx)
    elif data.startswith('sell_confirm_'):
        await show_sell_confirmation(q, ctx)
    elif data.startswith('sell_execute_'):
        await process_sell_execute(q, ctx)
    elif data.startswith('goto_'):
        await goto_location(q, ctx)
    elif data.startswith('fight_boss_'):
        await fight_boss(q, ctx)
    else:
        await q.answer()
        return

    await q.answer()

# ==================== API ДЛЯ MINI APP ====================

def verify_telegram_data(bot_token: str, init_data: str) -> dict | None:
    """
    Проверяет подпись данных, полученных от Telegram Web App.
    Возвращает объект user при успехе, иначе None.
    """
    try:
        data = dict(parse_qsl(init_data))
        received_hash = data.pop('hash', None)
        if not received_hash:
            logger.warning("No hash in init data")
            return None

        items = sorted(data.items())
        data_check_string = '\n'.join(f"{k}={v}" for k, v in items)

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(computed_hash, received_hash):
            logger.warning("Hash mismatch")
            return None

        user_str = data.get('user')
        if not user_str:
            logger.warning("No user field in init data")
            return None

        user = json.loads(user_str)
        return user
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return None

def rate_limit(max_requests: int, window: float = 1.0):
    """
    Декоратор для ограничения частоты запросов.
    max_requests – максимальное количество запросов в окне window (секунд).
    """
    def decorator(func):
        async def wrapper(request):
            # Извлекаем пользователя из initData
            init_data = request.headers.get('x-telegram-init-data')
            if not init_data:
                return JSONResponse({'error': 'Missing init data'}, status_code=401)
            user = verify_telegram_data(TOKEN, init_data)
            if not user:
                return JSONResponse({'error': 'Invalid init data'}, status_code=403)
            uid = user['id']

            now = time.time()
            # Получаем историю для этого пользователя
            history = request_history[uid]
            # Оставляем только запросы, попавшие в окно
            # Используем срез, чтобы не создавать новый список
            history[:] = [t for t in history if now - t < window]

            if len(history) >= max_requests:
                return JSONResponse({
                    'error': 'Too many requests. Please slow down.'
                }, status_code=429)

            # Добавляем текущий запрос
            history.append(now)
            # Ограничиваем размер истории (чтобы она не росла бесконечно)
            if len(history) > max_requests * 2:
                history[:] = history[-max_requests:]

            return await func(request)
        return wrapper
    return decorator

async def api_user(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)

    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    async with db_pool.acquire() as conn:
        await check_and_reset_bosses(conn)
        stats = await get_player_stats(uid, conn)
        inv = await get_inventory(uid, conn)
        current_location = await get_player_current_location(uid, conn)
        active_tool_id = await get_active_tool(uid, conn)
        active_tool_name = TOOLS.get(active_tool_id, {}).get('name', active_tool_id)

        boss_progress = {}
        rows = await conn.fetch("SELECT boss_id, current_health, defeated FROM boss_progress WHERE user_id = $1", uid)
        for row in rows:
            boss_progress[row['boss_id']] = {
                'current_health': row['current_health'],
                'defeated': row['defeated']
            }

    return JSONResponse({
        'id': uid,
        'level': stats['level'],
        'exp': stats['exp'],
        'gold': stats['gold'],
        'location': current_location,
        'inventory': inv,
        'upgrades': stats['upgrades'],
        'active_tool': active_tool_name,
        'boss_progress': boss_progress
    })

@app.route('/api/boss/attack', methods=['POST'])
@rate_limit(BOSS_ATTACK_LIMIT)
async def api_boss_attack(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)

    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']

    body = await request.json()
    boss_id = body.get('boss_id')
    if not boss_id or boss_id not in BOSS_LOCATIONS:
        return JSONResponse({'error': 'Invalid boss_id'}, status_code=400)

    bloc = BOSS_LOCATIONS[boss_id]

    async with db_pool.acquire() as conn:
        await check_and_reset_bosses(conn)
        async with conn.transaction():
            stats = await get_player_stats(uid, conn)
            if stats['level'] < bloc['min_level']:
                return JSONResponse({'error': 'Level too low'}, status_code=403)
            tool_level = await get_active_tool_level(uid, conn)
            if tool_level < bloc['min_tool_level']:
                return JSONResponse({'error': 'Tool level too low'}, status_code=403)

            prog_row = await conn.fetchrow(
                "SELECT current_health, defeated FROM boss_progress WHERE user_id = $1 AND boss_id = $2 FOR UPDATE",
                uid, boss_id
            )
            if not prog_row:
                await conn.execute(
                    "INSERT INTO boss_progress (user_id, boss_id, current_health) VALUES ($1, $2, $3)",
                    uid, boss_id, bloc['boss']['health']
                )
                current_health = bloc['boss']['health']
                defeated = False
            else:
                current_health = prog_row['current_health']
                defeated = prog_row['defeated']

            if defeated:
                return JSONResponse({'error': 'Boss already defeated'}, status_code=400)
            if current_health <= 0:
                return JSONResponse({'error': 'Boss already dead'}, status_code=400)

            # ----- НАЧАЛО НОВОГО КОДА (ЭФФЕКТЫ) -----
            effects = await get_active_effects(uid, conn)
            exp_multiplier = 1.0
            crit_bonus = 0
            for eff in effects.values():
                if 'exp_multiplier' in eff:
                    exp_multiplier *= eff['exp_multiplier']
                if 'crit_chance_bonus' in eff:
                    crit_bonus += eff['crit_chance_bonus']
            # ----- КОНЕЦ НОВОГО КОДА -----

            gold_damage, exp, is_crit = get_click_reward(stats)
            # Применяем эффекты
            exp = int(exp * exp_multiplier)
            if crit_bonus:
                extra_crit = random.random() < crit_bonus / 100
                if extra_crit and not is_crit:
                    is_crit = True
                    gold_damage *= 2
                    exp *= 2

            damage = gold_damage
            if is_crit:
                damage *= 2

            update_result = await conn.execute("""
                UPDATE boss_progress
                SET current_health = current_health - $1
                WHERE user_id = $2 AND boss_id = $3 AND current_health > 0
            """, damage, uid, boss_id)

            if update_result == "UPDATE 0":
                return JSONResponse({'error': 'Boss already defeated by another attack'}, status_code=409)

            new_health_row = await conn.fetchrow(
                "SELECT current_health FROM boss_progress WHERE user_id = $1 AND boss_id = $2",
                uid, boss_id
            )
            new_health = new_health_row['current_health']
            defeated_now = new_health <= 0

            loot_items = []
            if defeated_now:
                await conn.execute(
                    "UPDATE boss_progress SET defeated = TRUE WHERE user_id = $1 AND boss_id = $2",
                    uid, boss_id
                )

                boss = bloc['boss']
                gold_reward = boss['reward_gold']
                exp_reward = boss['exp_reward']

                loot_items.append(f"{gold_reward}💰")
                loot_items.append(f"{exp_reward}✨")

                await conn.execute(
                    "UPDATE players SET gold = gold + $1, exp = exp + $2 WHERE user_id = $3",
                    gold_reward, exp_reward, uid
                )
                await level_up_if_needed(uid, conn)

                for res, (min_amt, max_amt) in boss['reward_resources'].items():
                    amt = random.randint(min_amt, max_amt)
                    await add_resource(uid, res, amt, conn)
                    res_name = RESOURCES.get(res, {}).get('name', res)
                    loot_items.append(f"{res_name} x{amt}")

            new_stats = await get_player_stats(uid, conn)
            new_inv = await get_inventory(uid, conn)

    return JSONResponse({
        'damage': damage,
        'is_crit': is_crit,
        'defeated': defeated_now,
        'current_health': new_health,
        'max_health': bloc['boss']['health'],
        'new_gold': new_stats['gold'],
        'new_exp': new_stats['exp'],
        'inventory': new_inv,
        'loot': loot_items
    })

async def api_boss_info(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)
    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)
    uid = user['id']
    boss_id = request.path_params.get('boss_id')
    if not boss_id or boss_id not in BOSS_LOCATIONS:
        return JSONResponse({'error': 'Invalid boss_id'}, status_code=400)
    async with db_pool.acquire() as conn:
        await check_and_reset_bosses(conn)
        prog = await get_boss_progress(uid, boss_id, conn)
    return JSONResponse({
        'current_health': prog['current_health'],
        'defeated': prog['defeated'],
        'max_health': BOSS_LOCATIONS[boss_id]['boss']['health']
    })

@app.route('/api/click', methods=['POST'])
@rate_limit(CLICK_LIMIT)
async def api_click(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)

    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    result = await process_click(uid)
    return JSONResponse(result)

# ==================== ЗАПУСК ====================

async def run_bot():
    logger.info("Starting bot polling...")
    app_bot = Application.builder().token(TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("mine", cmd_mine))
    app_bot.add_handler(CommandHandler("locations", cmd_locations))
    app_bot.add_handler(CommandHandler("shop", cmd_shop))
    app_bot.add_handler(CommandHandler("tasks", cmd_tasks))
    app_bot.add_handler(CommandHandler("profile", cmd_profile))
    app_bot.add_handler(CommandHandler("inventory", cmd_inventory))
    app_bot.add_handler(CommandHandler("market", cmd_market))
    app_bot.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app_bot.add_handler(CommandHandler("faq", cmd_faq))
    app_bot.add_handler(CommandHandler("achievements", cmd_achievements))
    app_bot.add_handler(CommandHandler("help", cmd_help))
    app_bot.add_handler(CallbackQueryHandler(button_handler))
    app_bot.add_handler(CommandHandler("myid", cmd_myid))

    try:
        await app_bot.bot.delete_webhook(drop_pending_updates=True)
        await app_bot.initialize()
        await app_bot.start()
        await app_bot.updater.start_polling()
        logger.info("Bot polling started successfully")
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        logger.error(f"Error in bot polling: {e}", exc_info=True)
    finally:
        await app_bot.stop()

async def healthcheck(request):
    try:
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return JSONResponse({"status": "alive", "db": "ok"})
    except Exception as e:
        logger.error(f"Healthcheck DB error: {e}")
        return JSONResponse({"status": "alive", "db": "error"}, status_code=500)

async def api_craft_recipes(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)
    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    async with db_pool.acquire() as conn:
        inv = await get_inventory(uid, conn)
    recipes = []
    for rid, recipe in CRAFT_RECIPES.items():
        recipe_copy = recipe.copy()
        recipe_copy['id'] = rid
        recipe_copy['can_craft'] = all(inv.get(res, 0) >= need for res, need in recipe['resources'].items())
        recipe_copy['resources_available'] = {res: inv.get(res, 0) for res in recipe['resources']}
        recipes.append(recipe_copy)
    return JSONResponse({'recipes': recipes})

async def api_craft(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)
    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    body = await request.json()
    recipe_id = body.get('recipe_id')
    if not recipe_id or recipe_id not in CRAFT_RECIPES:
        return JSONResponse({'error': 'Invalid recipe_id'}, status_code=400)

    success, message = await craft_item(uid, recipe_id)
    if success:
        async with db_pool.acquire() as conn:
            new_inv = await get_inventory(uid, conn)
            new_items = await get_player_items(uid, conn)
            new_stats = await get_player_stats(uid, conn)
        return JSONResponse({
            'success': True,
            'message': message,
            'inventory': new_inv,
            'items': new_items,
            'gold': new_stats['gold'],
            'exp': new_stats['exp']
        })
    else:
        return JSONResponse({'success': False, 'message': message}, status_code=400)

async def api_items(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)
    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    async with db_pool.acquire() as conn:
        items_dict = await get_player_items(uid, conn)
        items_list = []
        for item_id, qty in items_dict.items():
            # Ищем рецепт по result_item_id
            recipe = next((r for r in CRAFT_RECIPES.values() if r['result_item_id'] == item_id), None)
            if recipe:
                items_list.append({
                    'id': item_id,
                    'name': recipe['name'],
                    'description': recipe['description'],
                    'quantity': qty,
                    'type': recipe['result_type'],
                    'effect': recipe.get('effect'),
                    'duration': recipe.get('duration')
                })
            else:
                # Если предмет не из рецептов (например, legacy)
                items_list.append({'id': item_id, 'quantity': qty, 'name': item_id})
    return JSONResponse({'items': items_list})

async def api_use_item(request):
    init_data = request.headers.get('x-telegram-init-data')
    if not init_data:
        return JSONResponse({'error': 'Missing init data'}, status_code=401)
    user = verify_telegram_data(TOKEN, init_data)
    if not user:
        return JSONResponse({'error': 'Invalid init data'}, status_code=403)

    uid = user['id']
    body = await request.json()
    item_id = body.get('item_id')
    quantity = body.get('quantity', 1)

    if not item_id:
        return JSONResponse({'error': 'Missing item_id'}, status_code=400)

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            cur_qty = await conn.fetchval(
                "SELECT quantity FROM player_items WHERE user_id = $1 AND item_id = $2",
                uid, item_id
            )
            if not cur_qty or cur_qty < quantity:
                return JSONResponse({'error': 'Not enough items'}, status_code=400)

            recipe = next((r for r in CRAFT_RECIPES.values() if r['result_item_id'] == item_id), None)
            if not recipe:
                return JSONResponse({'error': 'Unknown item'}, status_code=400)

            result_type = recipe.get('result_type')
            effect = recipe.get('effect', {})
            message = ""

            if result_type == 'key':
                boss_id = effect.get('boss_id')
                if boss_id and boss_id in BOSS_LOCATIONS:
                    max_hp = BOSS_LOCATIONS[boss_id]['boss']['health']
                    await conn.execute("""
                        UPDATE boss_progress
                        SET defeated = FALSE, current_health = $1
                        WHERE user_id = $2 AND boss_id = $3
                    """, max_hp, uid, boss_id)
                    message = f"🔑 Ключ использован, босс {BOSS_LOCATIONS[boss_id]['name']} снова доступен!"
                else:
                    return JSONResponse({'error': 'Invalid key effect'}, status_code=400)

            elif result_type == 'consumable':
                duration = recipe.get('duration', 0)
                if duration > 0:
                    await apply_effect(uid, item_id, effect, duration, conn)
                    message = f"⚗️ Использовано: {recipe['name']}"
                else:
                    # Если длительность не задана, считаем мгновенным эффектом (например, зелье лечения)
                    # Здесь можно добавить логику
                    message = f"⚗️ Использовано: {recipe['name']} (эффект применён)"

            elif result_type == 'permanent':
                if 'tool_power_bonus' in effect:
                    await conn.execute(
                        "UPDATE players SET perm_tool_power_bonus = perm_tool_power_bonus + $1 WHERE user_id = $2",
                        effect['tool_power_bonus'], uid
                    )
                if 'crit_chance_bonus_permanent' in effect:
                    await conn.execute(
                        "UPDATE players SET perm_crit_bonus = perm_crit_bonus + $1 WHERE user_id = $2",
                        effect['crit_chance_bonus_permanent'], uid
                    )
                message = f"⚔️ Модификатор {recipe['name']} применён постоянно."

            else:
                return JSONResponse({'error': 'Item type not usable'}, status_code=400)

            # Удаляем использованный предмет
            new_qty = cur_qty - quantity
            if new_qty == 0:
                await conn.execute(
                    "DELETE FROM player_items WHERE user_id = $1 AND item_id = $2",
                    uid, item_id
                )
            else:
                await conn.execute(
                    "UPDATE player_items SET quantity = $1 WHERE user_id = $2 AND item_id = $3",
                    new_qty, uid, item_id
                )

    return JSONResponse({'success': True, 'message': message})

async def startup_event():
    logger.info("Starting up...")
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    await init_db()
    asyncio.create_task(run_bot())

async def shutdown_event():
    logger.info("Shutting down...")
    if db_pool:
        await db_pool.close()

app = Starlette(
    routes=[Route("/healthcheck", healthcheck), Route("/", healthcheck)],
    on_startup=[startup_event],
    on_shutdown=[shutdown_event]
)

# Добавляем маршруты API
app.router.routes.extend([
    Route('/api/user', api_user, methods=['GET']),
    Route('/api/click', api_click, methods=['POST']),          # добавлено
    Route('/api/boss/attack', api_boss_attack, methods=['POST']), # добавлено
    Route('/api/boss/{boss_id}', api_boss_info, methods=['GET']),
    Route('/api/craft/recipes', api_craft_recipes, methods=['GET']),
    Route('/api/craft', api_craft, methods=['POST']),
    Route('/api/items', api_items, methods=['GET']),
    Route('/api/items/use', api_use_item, methods=['POST']),
])

# Добавляем CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Временно разрешаем все домены (для теста)
    allow_methods=["*"],
    allow_headers=["*"],
)

def main():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()








