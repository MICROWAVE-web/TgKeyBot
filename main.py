import json
import os
import random
import sys
import time
import traceback

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.payload import decode_payload
from decouple import config
import asyncio
import logging
from aiogram.filters import CommandObject, Command
from aiogram.exceptions import TelegramAPIError
from aiogram.exceptions import TelegramBadRequest
from aiogram import types
import redis.asyncio as redis

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
    kbrd = [[
        *[InlineKeyboardButton(text=f"Канал {ind}", url=f'https://t.me/{channel[1:]}') for ind, channel in
          enumerate(CHANNELS, start=1)],
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


async def send_key(user_id, from_ref=False):
    keys = get_keys()
    lkeys = len(keys)
    if lkeys <= int(config('KEYS_LEN_ALERT')):
        for admin in admins:
            try:
                await bot.send_message(int(admin), f'Внимание, осталось мало ключей: {lkeys}')
            except TelegramBadRequest:
                logging.warning('Telegram Bad Request')
    if keys:
        key = random.choice(keys)
        keys.remove(key)
        save_keys(keys)
        if from_ref:
            await bot.send_message(user_id, f'Ура, по реферальной ссылке перешли, держи подарок 🎁')
        await bot.send_message(user_id, f'Ваш ключ: {key}')
        return True
    else:
        await bot.send_message(user_id, 'Ключи закончились.')
        return False


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


async def main() -> None:
    # Инициализация Redis
    await init_redis()

    # Пропускаем накопившиеся апдейты при запуске
    await bot.delete_webhook(drop_pending_updates=True)

    # Запуск бота
    await dp.start_polling(bot)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())