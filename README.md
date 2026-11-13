# t-bot-sonar

Telegram-бот с командами `/start`, `/go`, `/buzz` и `/help`. Кнопка ведёт на сайт из `SITE_URL`. Бот использует long polling, поэтому входящий порт на сервере открывать не нужно. Он не начинает переписку первым и не сохраняет список пользователей.

## Запуск на Ubuntu через systemd

1. Создайте бота у `@BotFather` через `/newbot` и получите токен.
2. Создайте `/etc/t-bot-sonar.env` с переменными `BOT_TOKEN=...` и `SITE_URL=https://179.253.79.218/`, затем ограничьте доступ: `chmod 600 /etc/t-bot-sonar.env`.
3. Создайте системного пользователя `tbot`, скопируйте `t-bot-sonar.service` в `/etc/systemd/system/` и выполните `systemctl daemon-reload`, `systemctl enable --now t-bot-sonar`.
4. Проверьте журнал: `journalctl -u t-bot-sonar -f`, затем отправьте боту `/start`.

Для обновления кода: `git pull` в папке проекта и `systemctl restart t-bot-sonar`. Не коммитьте секретный файл и не публикуйте токен.

Тексты вовлечения меняются в списке `HOOKS` в `bot.py`. Для аналитики можно добавить UTM-параметры к `SITE_URL`.
