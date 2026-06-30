# news_bot.py

Simple Ethiopian news aggregator and Telegram publisher.

Requirements:

- Python 3.9+
- See `requirements.txt` for Python packages.

Usage:

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Set environment variables:

```bash
export TELEGRAM_BOT_TOKEN="<your-bot-token>"
export TELEGRAM_CHAT_ID="@yourchannel_or_chatid"
```

3. Run once (serverless / cron):

```bash
python news_bot.py --single-run
```

4. Or run as a daemon:

```bash
python news_bot.py
```

Configuration can be edited in the `SOURCES` dict in `news_bot.py`.

Docker
------

Build and run the container (optional):

```bash
docker build -t news-bot .
docker run --rm -e TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" -e TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" news-bot --single-run
```

Systemd
------

Copy `NEWS_BOT.service.example` to `/etc/systemd/system/news_bot.service`, edit `ExecStart` to set secrets or use an environment file, then enable and start with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now news_bot.service
```

CI
--

A GitHub Actions workflow is included at `.github/workflows/news-bot.yml` to run the bot on a schedule using repository secrets for credentials.