# SKR Photo Caption Bot

Telegram bot that reads a movie title and file URL from a photo caption, then replies to that photo with the fixed SKR caption template.

## Input format

Send a photo with a caption like this:

```text
ಕೂಲಿ (2025) ಕನ್ನಡ ಡಬ್ (HD)
https://example.com/movie
```

- First non-empty line: movie title.
- Next lines: include the file URL.
- The bot repeats the URL three times in the output.
- In Button Mode and Multi Button Mode, wrap text in `**` to make it bold; unmarked text uses the normal font. The bot converts these markers to Telegram HTML, so the asterisks do not appear in the posted caption.
- The output photo is sent as a reply to the original message.

## Deploy on Koyeb (Dockerfile)

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In [Koyeb](https://app.koyeb.com/), create a service from the GitHub repository `samrai6/CaptainA`.
3. Choose the `main` branch and Dockerfile/container build method. The repository includes a root-level `Dockerfile`.
4. Add an environment variable named `BOT_TOKEN` and set its value to the token from BotFather. Do not commit the token to GitHub.
5. Deploy the service. When it is running, open the bot in Telegram and send `/start`.

## Deploy on Render

1. Connect this repository to Render and create a Background Worker from `render.yaml`.
2. Set the environment variable `BOT_TOKEN` to your Telegram bot token.
3. Deploy the worker and send `/start` to the bot in Telegram.

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN="YOUR_BOT_TOKEN"
python bot.py
```

The bot will not run until a valid `BOT_TOKEN` is configured and the service is deployed.
