import asyncio
import json
import logging
import os
import random
import sys
import time

from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from decouple import config

API_TOKEN = config('API_TOKEN')
CHANNEL = config('CHANNEL')

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


def get_keyboard():
    bthurl = InlineKeyboardButton(text="Канал", url='https://t.me/instaponos')
    bthsub = InlineKeyboardButton(text="Проверить подписку", callback_data="subchennel")

    checksubmenu = InlineKeyboardMarkup(inline_keyboard=[[bthurl, bthsub]], resize_keyboard=True)
    return checksubmenu


def save_user_data(user_data):
    print(user_data)
    with open('users.json', 'w') as file:
        json.dump(user_data, file)


def save_keys(keys):
    with open(keys_file, 'w') as file:
        file.write('\n'.join(keys))


@dp.callback_query(F.data == 'subchennel')
@dp.message(CommandStart())
async def check_subscribe(message: types.Message):
    # Проверка подписки на канал
    current_time = time.time()
    try:
        chat_member = await bot.get_chat_member(chat_id=CHANNEL, user_id=message.from_user.id)
    except TelegramBadRequest:
        logging.error("Бот не состоит в канале!")
        await bot.send_message(message.from_user.id, 'Произошла ошибка, попробуйте позже')
        return

    if chat_member.status not in ['member', 'administrator', 'creator']:
        await bot.send_message(message.from_user.id,
                               'Чтобы получить ключ, вы должны быть подписаны на наш канал!',
                               reply_markup=get_keyboard())
        return
    else:
        await bot.send_message(message.from_user.id, 'Вы подписаны на канал!')

    users = get_users()

    # Проверка времени последнего получения ключа
    if str(message.from_user.id) in users:
        if current_time - users[str(message.from_user.id)][
            'last_key_time'] < 1209600:  # 2 недели в секундах
            await bot.send_message(message.from_user.id, 'Вы можете получить следующий ключ через 2 недели.')
            return
    current_time = time.time()

    keys = get_keys()

    # Выдача ключа
    if keys:
        key = random.choice(keys)
        keys.remove(key)
        save_keys(keys)
        users[str(message.from_user.id)] = {'last_key_time': current_time}
        save_user_data(users)
        await bot.send_message(message.from_user.id, f'Ваш ключ: {key}')
    else:
        await bot.send_message(message.from_user.id, 'Ключи закончились.')


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


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())
