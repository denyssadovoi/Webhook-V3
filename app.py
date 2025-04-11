import os
from flask import Flask, request
import telebot
from dotenv import load_dotenv

load_dotenv()  # To load environment variables

# === Configuration ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Your Seenode webhook URL

# Initialize the bot
bot = telebot.TeleBot(BOT_TOKEN)

# Flask application
app = Flask(__name__)

# Webhook endpoint for receiving updates
@app.route('/path', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return 'OK', 200

# Start the Flask server and set webhook
if __name__ == "__main__":
    bot.remove_webhook()  # Remove any existing webhooks
    bot.set_webhook(url=WEBHOOK_URL)  # Set the new webhook URL
    app.run(host='0.0.0.0', port=5000)
