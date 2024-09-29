**Шпаргалка**
```
адрес 193.124.47.207
пароль 
```
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
telegram ключ, id администраторов и имя файла с ключами хранятся в директориии проекта в файле .env```