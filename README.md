# SKR Photo Caption Bot

Telegram bot that takes a photo caption with a movie title on the first line and a file URL on the next line, then replies with the same photo and the fixed SKR caption template.

## Caption input

Send a photo with a caption like:

```text
ಕೂಲಿ (2025) ಕನ್ನಡ ಡಬ್ (HD)
https://example.com/movie
```

The bot repeats the URL three times. The `**` characters remain visible because Telegram parse mode is disabled.

## Deploy on Render

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Connect this repository to Render and create a Background Worker from `render.yaml`.
3. In Render, set the environment variable `BOT_TOKEN` to your bot token. Never commit the token to GitHub.
4. Deploy the worker, open the bot in Telegram, and send `/start`.

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN="YOUR_BOT_TOKEN"
python bot.py
```

The bot will not run until a valid token is configured and the worker is deployed.
