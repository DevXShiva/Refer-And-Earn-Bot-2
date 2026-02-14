---

# 🛍️ Referral & Coupon Telegram Bot

A high-performance, asynchronous **Telegram Bot** designed to automate coupon distribution through a viral referral system. Built with Python and MongoDB, it features a robust admin panel and mandatory channel subscription (FSub) to boost your community growth.

---

## ✨ Key Features

* **🔗 Viral Referral System:** Tracks unique invites and rewards users automatically.
* **📢 Forced Subscription (FSub):** Ensures users join your required channels before accessing the bot.
* **🎟️ Coupon Inventory Management:** Organized stock system for various denominations (500, 1000, 2000, 4000 ₪).
* **💎 Real-time Wallet:** Users can track their balance and redemption history instantly.
* **👑 Powerful Admin Panel:** Detailed statistics, bulk coupon uploading, and activity logs.
* **🚀 Deployment Ready:** Optimized for **Render** with a built-in Flask health-check server.

---

## 🛠️ Tech Stack

* **Language:** Python 3.10+
* **Framework:** `python-telegram-bot` (Asynchronous)
* **Database:** MongoDB (via Motor driver)
* **Web Server:** Flask (for 24/7 uptime on hosting platforms)

---

## ⚙️ Configuration

Set up the following environment variables in your `.env` file:

```env
BOT_TOKEN=your_bot_token
MONGO_URI=your_mongodb_uri
LOG_CHANNEL_ID=-100...
ADMIN_IDS=12345,67890
FSUB_CHANNEL_IDS=-100...,-100...
PORT=8080

```

---

## 🚀 Quick Start

1. **Clone & Install:**
```bash
git clone https://github.com/yourusername/shein-bot.git
cd shein-bot
pip install -r requirements.txt

```


2. **Run the Bot:**
```bash
python main.py

```



---

## 🎮 How it Works

### For Users

1. **Start:** User joins via a referral link.
2. **Verify:** Bot checks if the user has joined the required channels.
3. **Earn:** User shares their link to earn points (🧩).
4. **Redeem:** User swaps points for SHEIN coupon codes.

### For Admins

* Use `/admin` to view total users and active sessions.
* Add coupon codes in bulk by selecting the amount and pasting codes.
* Monitor all redemptions in the dedicated Log Channel.

---
## 🛡️ License

Distributed under the MIT License. See `LICENSE` for more information.

---

**Developed with ❤️ By SHIVA CHAUDHARY.**
