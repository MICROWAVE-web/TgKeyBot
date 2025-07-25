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

ALERT_DELAY = 3  # секунды между сообщениями
REPORT_EVERY = 25000  # как часто присылать отчёт админу

API_TOKEN = config('API_TOKEN')
CHANNELS = config('CHANNELS').split(',')

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

keys_file = config('KEYS_FILENAME')
admins = config('ADMINS').split(',')


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
                print(chat_member.status)
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
    await dp.start_polling(bot)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
