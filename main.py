import asyncio
import json
import logging
import os
import random
import ssl
import sys
import time
import traceback

import redis.asyncio as redis
from aiogram import Bot, Dispatcher, F
from aiogram import types
from aiogram.exceptions import TelegramAPIError
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, Command
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputFile, \
    FSInputFile
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.payload import decode_payload
from aiohttp import web
from decouple import config

# локальная блокировка по пользователю, чтобы не выдавать несколько ключей при спаме
user_locks = {}

LOCK_TTL = 5  # сек

ALERT_DELAY = 3  # секунды между сообщениями
REPORT_EVERY = 25000  # как часто присылать отчёт админу

API_TOKEN = config('API_TOKEN')
CHANNELS = config('CHANNELS').split(',')

# Redis для антиспама
REDIS_URL = config('REDIS_URL', default='redis://localhost:6379/0')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

keys_file = config('KEYS_FILENAME')
admins = config('ADMINS').split(',')

# Инициализация Redis
redis_client = None


async def init_redis():
    global redis_client
    try:
        redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logging.info("Redis connected successfully")
    except Exception as e:
        logging.error(f"Redis connection failed: {e}")
        redis_client = None


async def load_keys_to_redis():
    """Загрузка ключей из файла в Redis при старте, если список пуст."""
    if not redis_client:
        return
    list_len = await redis_client.llen('keys_list')
    if list_len and list_len > 0:
        return
    keys = get_keys()
    if keys:
        await redis_client.rpush('keys_list', *keys)
        logging.info(f"Loaded {len(keys)} keys into Redis list.")


async def acquire_user_lock(user_id: int):
    """Простая локальная блокировка, чтобы не спамили и не получали несколько ключей."""
    if user_id in user_locks:
        return False
    user_locks[user_id] = time.time()
    return True


async def release_user_lock(user_id: int):
    """Освобождение локальной блокировки"""
    try:
        user_locks.pop(user_id, None)
    except Exception:
        pass


class Throttled(Exception):
    pass


class CancelHandler(Exception):
    pass


class ThrottlingMiddleware:
    def __init__(self, rate_limit: float = 1.0, key_prefix: str = 'antiflood_'):
        self.rate_limit = rate_limit
        self.prefix = key_prefix

    async def __call__(self, handler, event: types.Message, data: dict):
        if not redis_client:
            return await handler(event, data)

        user_id = event.from_user.id
        key = f"{self.prefix}_{user_id}"

        try:
            await self.check_rate_limit(key)
        except Throttled:
            await event.answer("⚠️ Слишком много запросов. Пожалуйста, подождите немного.")
            return

        return await handler(event, data)

    async def check_rate_limit(self, key: str):
        if not redis_client:
            return

        now = time.time()
        data = await redis_client.hgetall(key)

        if data:
            last_call = float(data.get('last_call', 0))
            exceeded_count = int(data.get('exceeded_count', 0))
            delta = now - last_call

            if delta < self.rate_limit:
                exceeded_count += 1
                await redis_client.hset(key, mapping={
                    'last_call': now,
                    'exceeded_count': exceeded_count,
                    'delta': delta
                })
                raise Throttled()

        await redis_client.hset(key, mapping={
            'last_call': now,
            'exceeded_count': 0,
            'delta': 0
        })
        await redis_client.expire(key, 3600)  # Удаляем ключ через час


# Антиспам middleware для callback запросов
class CallbackThrottlingMiddleware:
    def __init__(self, rate_limit: float = 1.0):
        self.rate_limit = rate_limit

    async def __call__(self, handler, event: types.CallbackQuery, data: dict):
        if not redis_client:
            return await handler(event, data)

        user_id = event.from_user.id
        key = f"callback_antiflood_{user_id}"

        try:
            await self.check_rate_limit(key)
        except Throttled:
            await event.answer("⚠️ Слишком много кликов. Подождите секунду.", show_alert=True)
            return

        return await handler(event, data)

    async def check_rate_limit(self, key: str):
        if not redis_client:
            return

        now = time.time()
        last_call = await redis_client.get(key)

        if last_call:
            delta = now - float(last_call)
            if delta < self.rate_limit:
                raise Throttled()

        await redis_client.setex(key, int(self.rate_limit * 2), str(now))


# Регистрация middleware
message_throttle = ThrottlingMiddleware(rate_limit=2.0)  # 1 сообщение в 2 секунды
callback_throttle = CallbackThrottlingMiddleware(rate_limit=1.0)  # 1 колбэк в секунду

dp.message.middleware(message_throttle)
dp.callback_query.middleware(callback_throttle)


def get_keys():
    # Загрузка ключей из файла
    try:
        with open(keys_file, 'r') as file:
            keys = file.read().splitlines()
    except FileNotFoundError:
        logging.warning('Keys file not found')
        keys = []
    return keys


def get_users():
    # Загрузка данных о пользователях из файла
    try:
        with open('users.json', 'r') as file:
            user_data = json.load(file)
    except FileNotFoundError:
        logging.warning('Users file not found')
        user_data = {}
    return user_data


def get_keyboard(only_ref=False):
    if only_ref:
        bthref = KeyboardButton(text="Моя реферальная ссылка")
        return ReplyKeyboardMarkup(keyboard=[[bthref]], resize_keyboard=True)

    channels_invite_str = []
    for channel in CHANNELS:
        if '|' in channel:
            channels_invite_str.append(channel.rstrip("|").split("|")[-1])
        else:
            channels_invite_str.append(channel.lstrip("@"))

    kbrd = [[
        *[InlineKeyboardButton(text=f"Канал {ind}", url=f'https://t.me/{channel_invite}') for ind, channel_invite in
          enumerate(channels_invite_str, start=1)],
        InlineKeyboardButton(text="Проверить подписку", callback_data="subchennel")
    ]]

    return InlineKeyboardMarkup(inline_keyboard=kbrd, resize_keyboard=True)


def save_user_data(user_data):
    with open('users.json', 'w') as file:
        json.dump(user_data, file)


def save_keys(keys):
    with open(keys_file, 'w') as file:
        file.write('\n'.join(keys))


# хендлер для создания ссылок
@dp.message(F.text.startswith("Моя реферальная ссылка"))
async def get_ref(message: types.Message):
    # Дополнительная проверка частоты запросов для реферальных ссылок
    if redis_client:
        user_id = message.from_user.id
        key = f"ref_link_{user_id}"
        last_request = await redis_client.get(key)

        if last_request:
            delta = time.time() - float(last_request)
            if delta < 30:  # Не чаще чем раз в 30 секунд
                await message.answer("⚠️ Ссылку можно запрашивать не чаще чем раз в 30 секунд.")
                return

        await redis_client.setex(key, 30, str(time.time()))

    link = await create_start_link(bot, str(message.from_user.id), encode=True)
    await bot.send_message(message.from_user.id, f"Ваша реф. ссылка {link}")


async def send_key(user_id: int, from_ref=False):
    if not await acquire_user_lock(user_id):
        await bot.send_message(user_id, "⚠️ Ваш запрос уже обрабатывается. Подождите пару секунд.")
        return False

    try:
        key = None
        if redis_client:
            key = await redis_client.lpop('keys_list')  # атомарная выдача

        if not key:
            # fallback на файл (на случай сбоя Redis)
            keys = get_keys()
            if not keys:
                await bot.send_message(user_id, 'Ключи закончились.')
                return False
            key = random.choice(keys)
            keys.remove(key)
            save_keys(keys)

        # проверка остатка ключей
        if redis_client:
            lkeys = await redis_client.llen('keys_list')
        else:
            lkeys = len(get_keys())

        if lkeys <= int(config('KEYS_LEN_ALERT')):
            for admin in admins:
                try:
                    await bot.send_message(int(admin), f'Внимание, осталось мало ключей: {lkeys}')
                except TelegramBadRequest:
                    logging.warning('Telegram Bad Request')

        if from_ref:
            await bot.send_message(user_id, f'Ура, по реферальной ссылке перешли, держи подарок 🎁')

        await bot.send_message(user_id, f'Ваш ключ: {key}')
        return True

    finally:
        await release_user_lock(user_id)


# Временное хранилище обработки команд
active_processes = set()  # Используем set для хранения ID пользователей, чтобы отслеживать активные процессы


@dp.callback_query(F.data == 'subchennel')
@dp.message(CommandStart())
async def check_subscribe(message: types.Message, command: CommandObject = None):
    user_id = str(message.from_user.id)
    current_time = time.time()
    users = get_users()

    # Проверяем, если команда уже в процессе выполнения
    if user_id in active_processes:
        await bot.send_message(message.from_user.id, "Ваш запрос уже обрабатывается. Пожалуйста, подождите.")
        return

    # Устанавливаем флаг процесса
    active_processes.add(user_id)

    try:
        # Проверяем, если пользователь уже зарегистрирован
        if user_id not in users:
            await bot.send_message(message.from_user.id,
                                   '''
🙏 Привет, старина! Я РобоГабен, щедрый бот, который раздает ключи от игр Steam совершенно бесплатно каждые 2 недели. 

▫️Для получения ключей, нужно быть подписанным на [меня](https://t.me/gabenson) и на [Халявный Steam](https://t.me/SteamByFree)

▫️Мой создатель: [Cын Габена](http://t.me/gabenson)
▫️По техническим вопросам, обращайтесь: @sh33shka                           
                                   ''', parse_mode="MARKDOWN")
            referal = ""
            if command and command.args:
                reference = str(decode_payload(command.args))
                if reference != user_id:  # Исключаем реферальную ссылку на самого себя
                    referal = reference

            users[user_id] = {'referal': referal}
            save_user_data(users)

        # Проверка подписки
        all_in = True
        for channel in CHANNELS:

            try:
                chat_member = await bot.get_chat_member(chat_id=channel, user_id=message.from_user.id)
                if chat_member.status not in ['member', 'administrator', 'creator']:
                    all_in = False
                    break
            except TelegramBadRequest:
                traceback.print_exc()
                all_in = False
                break

        if not all_in:
            await bot.send_message(message.from_user.id,
                                   'Чтобы получить ключ, вы должны быть подписаны на наш канал!',
                                   reply_markup=get_keyboard())
            return

        await bot.send_message(message.from_user.id, 'Вы подписаны на каналы!',
                               reply_markup=get_keyboard(only_ref=True))

        # Проверка реферальной системы
        referal = users[user_id].get('referal', "")
        if referal and referal.isdigit():
            if 'last_ref_time' not in users[referal] or current_time - users[referal]['last_ref_time'] >= 1:
                await send_key(int(referal), from_ref=True)
                users[user_id]['referal'] = ""
                users[referal]['last_ref_time'] = current_time
                save_user_data(users)

        # Проверка времени последнего получения ключа
        if 'last_key_time' in users[user_id] and current_time - users[user_id]['last_key_time'] < 1209600:
            # if 'last_key_time' in users[user_id]:
            await bot.send_message(message.from_user.id, 'Вы уже получили ключ.')
            return

        # Выдача ключа
        if await send_key(message.from_user.id):
            users[user_id]['last_key_time'] = current_time
            save_user_data(users)

    finally:
        # Снимаем флаг обработки
        active_processes.discard(user_id)


@dp.message(F.document)
async def handle_docs(message: types.Message):
    if str(message.from_user.id) in admins:
        document = message.document
        if document.file_name == keys_file:
            file_info = await bot.get_file(document.file_id)
            file_path = file_info.file_path
            await bot.download_file(file_path, 'new_keys.txt')

            with open('new_keys.txt', 'r') as file:
                new_keys = file.read().splitlines()

            # Добавляем ключи в Redis
            if redis_client:
                if new_keys:
                    await redis_client.rpush('keys_list', *new_keys)

            keys = get_keys()
            for nkew in new_keys:
                if nkew not in keys:
                    keys.append(nkew)

            with open(keys_file, 'w') as file:
                file.write('\n'.join(keys))

            os.remove('new_keys.txt')
            await message.reply('Ключи успешно обновлены.')
        else:
            await message.reply('Неверный файл. Пожалуйста, отправьте файл с именем keys.txt.')
    else:
        await message.reply('У вас нет прав для выполнения этой команды.')


@dp.message(Command(commands=['alert']))
async def cmd_alert(message: types.Message, command: CommandObject):
    user_id = str(message.from_user.id)
    if user_id not in admins:
        return await message.reply("❌ У вас нет прав для выполнения этой команды.")
    if not command.args:
        return await message.reply("Использование: /alert <текст рассылки>")

    text = command.args
    await message.reply("📨 Рассылка началась. Она займёт около 4 дней.")
    await asyncio.create_task(alert_background(text, message.from_user.id))


async def alert_background(text: str, admin_id: int):
    users = get_users()
    total = len(users)
    sent = 0
    failed = 0

    for idx, (uid, _) in enumerate(users.items(), start=1):
        try:
            await bot.send_message(chat_id=int(uid), text=text)
            sent += 1
        except TelegramBadRequest as e:
            logging.warning(f"BadRequest при отправке {uid}: {e}")
            failed += 1
        except TelegramAPIError as e:
            if "Too Many Requests" in str(e):
                logging.warning(f"Превышен лимит. Пауза 5 секунд.")
                await asyncio.sleep(5)
                try:
                    await bot.send_message(chat_id=int(uid), text=text)
                    sent += 1
                except Exception as ex:
                    failed += 1
                    logging.error(f"Ошибка при повторной отправке {uid}: {ex}")
            else:
                failed += 1
                logging.error(f"API ошибка: {e}")
        except Exception as e:
            logging.error(f"Ошибка при отправке {uid}: {e}")
            failed += 1

        if idx % REPORT_EVERY == 0:
            await bot.send_message(
                chat_id=admin_id,
                text=f"📊 Промежуточный отчёт: {sent} отправлено, {failed} ошибок из {idx} обработанных.",
            )

        await asyncio.sleep(ALERT_DELAY)

    await bot.send_message(
        chat_id=admin_id,
        text=f"✅ Рассылка завершена. Всего: {sent} отправлено, {failed} ошибок, из {total} пользователей.",
    )


# Webhook configuration
WEBHOOK_HOST = config('WEBHOOK_HOST', default='https://robogaben.ru')
WEBHOOK_PATH = config('WEBHOOK_PATH', default='/webhook')
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

SSL_CERT = config('SSL_CERT', default='webhook.pem')
SSL_KEY = config('SSL_KEY', default='webhook.key')
WEBHOOK_PORT = int(config('WEBHOOK_PORT', default=8443))


async def on_startup(app):
    await init_redis()
    await load_keys_to_redis()
    cert = FSInputFile(SSL_CERT)
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True, certificate=cert)

    logging.info(f"Webhook set to {WEBHOOK_URL}")


async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("Webhook removed")


async def handle_webhook(request):
    update = await request.json()
    update = types.Update(**update)
    await dp.feed_update(bot, update)
    return web.Response()


def main():
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(SSL_CERT, SSL_KEY)

    web.run_app(
        app,
        host='0.0.0.0',
        port=WEBHOOK_PORT,
        ssl_context=ssl_context
    )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    main()
