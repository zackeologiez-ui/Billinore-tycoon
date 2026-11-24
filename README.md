# 💰 Millionaire Tycoon 3D

A 3D online multiplayer tycoon game. Build your money empire, trade stocks and crypto on your phone, chat with players and bots, and create your own servers with room codes.

## How to run

1. Install [Python 3](https://www.python.org/downloads/). No extra packages are needed.
2. Download this repo (green **Code** button → **Download ZIP**) and unzip it.
3. Open a terminal in the folder and run:
   ```
   python server.py
   ```
4. Open **http://localhost:8000** in your browser.

Friends on the same Wi-Fi can join with `http://YOUR-PC-IP:8000`.

**Admin menu (T key):** the first time the server starts, it makes a random admin password, saves it in `admin_password.txt`, and prints it in the terminal. Edit that file to pick your own password.

## Controls

| Key | Action |
|-----|--------|
| WASD | Move |
| Space | Jump / fly up |
| Left Shift | Fly down |
| P | Phone (32 apps) |
| MSG button | Chat |

## Files

- `index.html`: the game (Three.js)
- `server.py`: multiplayer server (Python, no dependencies)
- `three.min.js`: Three.js r128
