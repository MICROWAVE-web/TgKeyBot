# Телеграмм бот для проверки подписки на каналы и выдачи ключей Steam 


## Содержание
- [Начало работы](#начало-работы)
- [Команда проекта](#команда-проекта)

## Технологии
- [Aiogram](https://aiogram.dev/)

## Начало работы

### Требования
Для установки и запуска проекта, необходим [Python](https://www.python.org/) v3.10

**
Директория
```
/home/git/TgKeyBot/
```
> 
Виртуальное окружение
```
source env/bin/activate
```

Файл Сервиса
```
/lib/systemd/system/tgkeybot.service
```

Логи
```
sudo journalctl -u tgkeybot.service
```

Старт, Перезапуск, остановка сервиса
```
sudo systemctl start tgkeybot
sudo systemctl restart tgkeybot
sudo systemctl stop tgkeybot
```


Подтягивание изменений на сервере:
```
cd /home/git/TgKeyBot/
git pull 
```


```
telegram ключ, id администраторов и имя файла с ключами хранятся в директориии проекта в файле .env:


# Токен бота. p.s. бот должен находиться в канале
API_TOKEN=00000:abcabcabc

# Канал, с @
CHANNELS=@abc2,@abc1

# Название файла с ключами
KEYS_FILENAME=keys.txt

# Администраторы, с разрешением дополнять ключи. Если несколько, то через запятую
ADMINS=123123123


# Количество оставшихся ключей, после которого уведомления приходят администраторам
KEYS_LEN_ALERT=20

```

## Команда проекта
- [Кованов Алексей (Я)](https://t.me/kovanoFFFreelance) — FullStack Engineer

