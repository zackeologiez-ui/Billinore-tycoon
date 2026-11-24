#!/usr/bin/env python3
"""
99999X MILLIONAIRE SIMULATOR - online multiplayer server
Pure Python standard library: nothing to install.

It serves the game page AND runs the online lobby (players, chat, NPCs, admin)
over WebSockets.

    Run:   python server.py            (another port:  set PORT=9000 then run)
    Play:  http://localhost:8000
    Friends on the same Wi-Fi:  http://<your LAN IP>:8000   (printed at startup)
"""
import asyncio, base64, collections, datetime, hashlib, json, math, os, random, re, socket, struct, time

try:
    import anthropic  # pip install anthropic  (only needed for Claude-powered NPC chat)
except ImportError:
    anthropic = None

PORT = int(os.environ.get('PORT', '8000'))
ROOT = os.path.dirname(os.path.abspath(__file__))


def load_admin_password():
    """Admin password: ADMIN_PASSWORD env var, else admin_password.txt.
    If neither exists, a random one is made and saved to admin_password.txt
    (that file is in .gitignore, so it never ends up on GitHub)."""
    env = os.environ.get('ADMIN_PASSWORD', '').strip()
    if env:
        return env
    path = os.path.join(ROOT, 'admin_password.txt')
    try:
        with open(path, encoding='utf-8') as f:
            pw = f.read().strip()
        if pw:
            return pw
    except OSError:
        pass
    import secrets
    pw = secrets.token_urlsafe(9)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(pw + '\n')
    except OSError:
        pass
    return pw


ADMIN_PASSWORD = load_admin_password()
KEY_FILE = os.path.join(ROOT, 'claude_api_key.txt')
OPENAI_KEY_FILE = os.path.join(ROOT, 'openai_api_key.txt')
AI_MODEL = 'claude-opus-5-5'
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
AI = {'client': None, 'enabled': False, 'bots': True, 'busy': set(), 'err': '', 'provider': None}   # bots use built-in replies; admin can switch AI chat on
SAVE_FILE = os.path.join(ROOT, 'npc_save.json')
FILES = {'/': ('index.html', 'text/html; charset=utf-8'),
         '/index.html': ('index.html', 'text/html; charset=utf-8'),
         '/three.min.js': ('three.min.js', 'application/javascript; charset=utf-8')}
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
TICK = 0.1

# ---------------- world layout (must match index.html) ----------------
NPLOTS, RING = 14, 110
NPC_PLOTS = [0, 2, 4, 7, 9, 11]
PLAYER_PLOTS = [1, 3, 5, 6, 8, 10, 12, 13]
COLS = [-15, -5, 5, 15]
ROWS = [-14, -2, 10]
BIZ = [('Lemonade Stand', 15, 0.5), ('Food Truck', 120, 4), ('Car Wash', 1300, 20), ('Crypto Farm', 14000, 110),
       ('Tech Startup', 2e5, 900), ('Oil Empire', 3e6, 8000), ('Bank Chain', 5e7, 7e4), ('AI Megacorp', 8e8, 6e5),
       ('Space Mining', 1.2e10, 5e6), ('Moon Casino', 2e11, 4.5e7), ('Time Machine', 4e12, 4e8), ('Multiverse Bank', 8e13, 4e9)]
BENCHES = [(math.cos(k * math.pi / 4 + math.pi / 8) * 30, math.sin(k * math.pi / 4 + math.pi / 8) * 30) for k in range(8)]
KIOSK = (math.cos(-math.pi / 4) * 16, math.sin(-math.pi / 4) * 16)
ROAD_R = 75


def plot_frame(i):
    a = i / NPLOTS * 2 * math.pi
    return math.cos(a) * RING, math.sin(a) * RING, math.atan2(-math.cos(a), -math.sin(a))


def to_world(i, lx, lz):
    cx, cz, th = plot_frame(i)
    c, s = math.cos(th), math.sin(th)
    return cx + lx * c + lz * s, cz - lx * s + lz * c


def to_local(i, x, z):
    cx, cz, th = plot_frame(i)
    dx, dz = x - cx, z - cz
    c, s = math.cos(th), math.sin(th)
    return dx * c - dz * s, dx * s + dz * c


SUF = ['', 'K', 'M', 'B', 'T', 'Qa', 'Qi', 'Sx', 'Sp', 'Oc', 'No', 'Dc', 'UDc', 'DDc', 'TDc', 'QaDc', 'QiDc', 'SxDc', 'SpDc', 'OcDc', 'NoDc', 'Vg']


def fmt(n):
    if n != n or n == float('inf'):
        return 'inf'
    if n < 1000:
        return str(int(max(n, 0)))
    e = int(math.log10(n) // 3)
    if e < len(SUF):
        v = n / 10 ** (3 * e)
        return (f"{v:.1f}" if v >= 100 else f"{v:.2f}") + SUF[e]
    return f"{n:.2e}"


def current_build():
    """Read BUILD='...' from index.html so out-of-date pages can be told to reload."""
    try:
        with open(os.path.join(ROOT, 'index.html'), encoding='utf-8') as f:
            mt = re.search(r"const BUILD='([^']+)'", f.read(200000))
        return mt.group(1) if mt else None
    except OSError:
        return None


def num(v, d=0.0, lo=-1e300, hi=1e300):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return d
    if v != v or v in (float('inf'), float('-inf')):
        return d
    return max(lo, min(hi, v))


pick = random.choice

# ---------------- stock / crypto market (shared by everyone) ----------------
MARKET = [('CLAP', 'AmaClap Inc', '📦', 'stock', 120, 0.012), ('BURG', 'BurgerBoss', '🍔', 'stock', 45, 0.010),
          ('ROKT', 'RocketYard', '🚀', 'stock', 300, 0.020), ('GAME', 'PixelForge Games', '🎮', 'stock', 80, 0.015),
          ('BANK', 'MegaBank99', '🏦', 'stock', 150, 0.008), ('FIZZ', 'FizzCo Soda', '🥤', 'stock', 25, 0.010),
          ('BRNX', 'BrainBox AI', '🤖', 'stock', 420, 0.025), ('LUX', 'Luxury Index', '💎', 'stock', 100, 0.006),
          ('HOME', 'HomeFund REIT', '🏠', 'stock', 100, 0.005),
          ('BITC', 'BitCash', '🪙', 'crypto', 30000, 0.030), ('WOOF', 'WoofCoin', '🐶', 'crypto', 0.25, 0.060),
          ('MOON', 'MoonToken', '🌙', 'crypto', 3.5, 0.080), ('NINE', '99999Coin', '💠', 'crypto', 9.99, 0.050)]
MK = {s[0]: {'p': float(s[4]), 'base': float(s[4]), 'vol': s[5], 'name': s[1], 'h': collections.deque([float(s[4])] * 60, maxlen=120)} for s in MARKET}
NEWS_UP = ["{n} smashes earnings records", "{n} signs a mega deal with the Moon", "influencers can't stop buying {n}", "{n} launches a viral new product", "whales are scooping up {n}"]
NEWS_DOWN = ["{n} CEO caught napping on livestream", "{n} servers crash for 6 hours", "{n} recall: products explode (safely)", "scandal hits {n}", "{n} misses earnings badly"]


def market_tick():
    for m in MK.values():
        m['base'] *= math.exp(0.0002)                       # slow bull market
        rev = 0.004 * math.log(m['base'] / m['p'])          # mean reversion keeps prices sane
        m['p'] = max(m['base'] * 0.05, min(m['base'] * 20, m['p'] * math.exp(rev + m['vol'] * random.gauss(0, 1))))
        m['h'].append(round(m['p'], 6))


def market_news():
    sym = pick(list(MK)); m = MK[sym]
    up = random.random() < 0.55; mag = random.uniform(0.10, 0.35)
    m['p'] *= (1 + mag) if up else (1 - mag * 0.8)
    head = pick(NEWS_UP if up else NEWS_DOWN).format(n=m['name'])
    sys_chat(f"📰 BREAKING: {head}! {sym} {'+' if up else '-'}{round(mag * (100 if up else 80))}%", 'news')


def market_init_msg():
    return {'t': 'mkt_init', 'list': [{'s': s[0], 'n': s[1], 'i': s[2], 'k': s[3]} for s in MARKET],
            'h': {k: list(m['h']) for k, m in MK.items()}}


CLIENTS = []          # every websocket connection (in a room or still on the menu)
NEXT_ID = [1]
MAX_PLAYERS = len(PLAYER_PLOTS)


# ---------------- rooms (servers) ----------------
class Room:
    def __init__(self, code, name, owner='', public=True, bots=True):
        self.code = code; self.name = name; self.owner = owner; self.public = public
        self.bots = bots; self.npcs = new_npcs(); self.pending = []
        self.chat_log = collections.deque(maxlen=14); self.banned = {}
        self.created = time.time(); self.empty_since = time.time()
        self.chat_t = time.time() + 8; self.news_t = time.time() + 40

    def info(self):
        players = [c for c in CLIENTS if c.joined and c.room is self]
        return {'code': self.code, 'name': self.name, 'owner': self.owner, 'players': len(players), 'max': MAX_PLAYERS,
                'public': self.public, 'bots': self.bots, 'main': self.code == 'MAIN', 'names': [c.name for c in players][:8]}


ROOMS = {}
MAIN = None
R = None              # the room currently being processed (single-threaded asyncio, so this is safe)


def use(room):
    global R
    R = room


def new_code():
    while True:
        code = ''.join(random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(5))
        if code not in ROOMS: return code


def reset_bots(room):
    """Wipe all bot progress in a room (fresh NPCs)."""
    room.npcs = new_npcs(); room.pending.clear()
    if room is MAIN:
        try: os.remove(SAVE_FILE)
        except OSError: pass


def joined():
    return [c for c in CLIENTS if c.joined and c.room is R]


def client_by_id(cid):
    for c in joined():
        if c.id == cid:
            return c
    return None


def everyone():
    return [(c.name, c.cash) for c in joined()] + ([(n.name, n.cash) for n in R.npcs] if R.bots else [])


# ---------------- WebSocket plumbing ----------------
def frame(payload, op=0x1):
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    if n < 126:
        hdr = struct.pack('>BB', 0x80 | op, n)
    elif n < 65536:
        hdr = struct.pack('>BBH', 0x80 | op, 126, n)
    else:
        hdr = struct.pack('>BBQ', 0x80 | op, 127, n)
    return hdr + payload


async def ws_read(reader):
    h = await reader.readexactly(2)
    op, masked, ln = h[0] & 0x0F, h[1] & 0x80, h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack('>H', await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack('>Q', await reader.readexactly(8))[0]
    if ln > 65536:
        raise ConnectionError('frame too big')
    mask = await reader.readexactly(4) if masked else b'\0\0\0\0'
    data = await reader.readexactly(ln)
    if masked and ln:
        m = (mask * (ln // 4 + 1))[:ln]
        data = (int.from_bytes(data, 'big') ^ int.from_bytes(m, 'big')).to_bytes(ln, 'big')
    return op, data


class Client:
    def __init__(self, writer):
        self.w = writer
        self.id = None; self.name = '?'; self.joined = False; self.admin = False
        self.x = self.y = self.z = 0.0; self.ry = 0.0; self.a = 'idle'
        self.cash = 0.0; self.b = [0] * 12; self.plot = None; self.color = '#00ff88'; self.od = 0; self.reb = 0
        self.last_chat = 0; self.admin_tries = []; self.room = None

    def raw(self, data):
        try:
            t = self.w.transport
            if t.is_closing():
                return
            if t.get_write_buffer_size() > 2_000_000:
                self.w.close(); return
            self.w.write(data)
        except Exception:
            pass

    def send(self, obj):
        self.raw(frame(json.dumps(obj, separators=(',', ':'))))


def broadcast(obj, skip=None):
    if obj.get('t') == 'chat':
        R.chat_log.append((obj.get('name') + ': ' if obj.get('name') else '[system] ') + str(obj.get('text', '')))
    data = frame(json.dumps(obj, separators=(',', ':')))
    for c in joined():
        if c is not skip:
            c.raw(data)


def sys_chat(text, kind='sys'):
    broadcast({'t': 'chat', 'kind': kind, 'text': text})


# ---------------- NPC personalities ----------------
PERSONAS = [
    dict(name='CryptoKing99', style='hype', color='#f5b301', alias=['crypto', 'cryptoking', 'king'], age=19, home='Miami',
         job='crypto trader', pet='a husky named Bitcoin', food='sushi', music='trap', fav='Crypto Farm'),
    dict(name='DubaiPrince', style='rich', color='#f8fafc', alias=['dubai', 'prince'], age=24, home='Dubai',
         job="running my dad's oil company", pet='a white cat named Gold', food='wagyu steak', music='classical', fav='Oil Empire'),
    dict(name='xX_Slayer_Xx', style='toxic', color='#ff3b5c', alias=['slayer'], age=14, home='Texas',
         job='school (ugh)', pet='a lizard named Kevin', food='pizza rolls', music='phonk', fav='Moon Casino'),
    dict(name='QuantumQueen', style='nerd', color='#a855f7', alias=['queen', 'qq', 'quantumqueen'], age=22, home='Boston',
         job='physics student', pet='a cat named Schrodinger', food='ramen', music='lofi', fav='Time Machine'),
    dict(name='ChillVibesJay', style='chill', color='#38bdf8', alias=['jay', 'chillvibes', 'vibes'], age=27, home='California',
         job='surf instructor', pet='a golden retriever named Taco', food='tacos', music='reggae', fav='Food Truck'),
    dict(name='HodlHannah', style='friendly', color='#f472b6', alias=['hannah', 'hodl'], age=17, home='London',
         job='part-time barista', pet='two bunnies', food='pasta', music='pop', fav='Lemonade Stand'),
]

INTENTS = [
    ('bot', r"\b(are|r) (you|u) (a |an )?(bot|npc|ai|robot|real|fake|human)\b|^(bot|npc)\?"),
    ('follow', r"\bfollow (me|us)\b"),
    ('come', r"\b(come|get) (here|over here|to me|over)\b|\bcome to me\b"),
    ('stop', r"\b(stop|stay|wait|freeze|stand still|chill out)\b"),
    ('leave', r"\b(go away|leave me|get lost|shoo|bye|cya|goodbye|gtg)\b"),
    ('dance', r"\b(dance|dancing|emote)\b"),
    ('sit', r"\bsit\b"),
    ('jog', r"\b(race|jog|run with me)\b"),
    ('gift', r"\b(give|gimme|send|donate|lend|spare)\b.*\b(money|cash|bucks|\$|some|me)\b|\bcan i (have|get) (some )?(money|cash)\b|\bmoney pl(s|ease|z)\b"),
    ('mymoney', r"\bhow much (money |cash )?(do )?i (have|got)\b|\bam i rich\b"),
    ('money', r"\bhow (much|rich)\b.*\b(you|u|ur)\b|\b(your|ur) (net ?worth|money|cash)\b"),
    ('richest', r"\bwho('?s| is)?\b.*\b(richest|#1|number one|number 1|top|winning|first place)\b"),
    ('doing', r"\b(what|wat|wut)('?s| are| r)? (you|u|ya) (doing|doin|up to)\b|\bwyd\b"),
    ('from', r"\bwhere (are|r) (you|u) from\b|\bwhere do (you|u) live\b"),
    ('where', r"\bwhere (are|r) (you|u)\b|\bwhere u at\b"),
    ('how', r"\bhow (are|r) (you|u|ya)\b|\bhru\b|\bhow('?s| is) it going\b|\bwhats up\b|\bwassup\b|\bsup\b"),
    ('age', r"\bhow old\b|\b(ur|your) age\b"),
    ('time', r"\bwhat time\b|\btime is it\b"),
    ('food', r"\b(eat|eating|food|hungry|lunch|dinner|breakfast)\b"),
    ('job', r"\b(job|school|homework|class|irl)\b"),
    ('pet', r"\b(pet|pets|dog|cat)\b"),
    ('music', r"\b(music|song|songs|listen)\b"),
    ('weather', r"\b(weather|raining|sunny|hot|cold)\b"),
    ('sleep', r"\b(sleep|tired|bed|bedtime)\b"),
    ('tips', r"\b(tip|tips|help|how do i|how to|how do u|how do you|advice|guide)\b"),
    ('rob', r"\b(rob|steal|heist|thief|robbed)\b"),
    ('insult', r"\b(noob|trash|ugly|loser|stupid|dumb|idiot|cringe|suck|sucks|bozo|broke|bad)\b"),
    ('nice', r"\b(cool|nice|awesome|love|goat|amazing|legend|pro|best)\b"),
    ('thanks', r"\b(thanks|thank you|thx|ty)\b"),
    ('greet', r"\b(hi|hello|hey|yo|hii+|heyy+|howdy|gm|hola)\b"),
    ('laugh', r"\b(lol|lmao|haha+|xd|lmfao)\b|😂|🤣"),
    ('fav', r"\b(fav|favorite|favourite)\b"),
    ('team', r"\b(team|friends?|ally|squad|trade)\b"),
    ('yes', r"^(yes|yeah|ya|yep|ok|okay|sure)\b"),
    ('no', r"^(no|nah|nope)\b"),
]

TIPS = ["walk onto the glowing pads at your tycoon to buy stuff", "stand on the gold COLLECT pad to grab your money",
        "save for the Quantum Printer, its x99999", "rebirth stacks x99999 forever", "press Q when overdrive is full",
        "buy laser grid + bodyguards so ppl cant rob u", "rob the richest people, press F next to them",
        "golden briefcases spawn near you, walk into them", "hit the green zone when robbing for +25%"]

GENERIC_CHATTER = ["anyone else grinding rn", "this server is so fun", "who wants to team up", "my tycoon is looking clean ngl",
                   "TO THE MOON 🚀", "i need a billion asap", "who keeps robbing people lol", "overdrive + diamond hands is crazy",
                   "brb getting a snack", "the plaza is so nice today", "i'm saving up for something big", "gg everyone",
                   "anyone seen a golden briefcase?", "lag?? or just me",
                   # --- extra chatter ---
                   "who wants to race around the road?", "my lemonade stand is basically a bank now", "just bought a gold watch on amaclap ⌚",
                   "stocks are going crazy today 📈", "never selling my WoofCoin 🐶", "who else is saving for a sports car?",
                   "the smoothie kiosk is my second home", "imagine being poor lol jk ily all", "rebirth or keep grinding? 🤔",
                   "my bodyguards just blocked a robbery 😎", "this game needs a pizza shop fr", "who has the biggest tycoon rn?",
                   "i just tripped over a tree 😭", "buy low sell high, thats the secret", "anyone wanna trade tips?",
                   "my crypto farm is printing money ⛏️", "the tower leaderboard is calling my name", "just got robbed AGAIN bro 😤",
                   "what time is it irl? i forgot to eat", "my phone battery is always 99% lol", "AmaClap delivery is so fast 📦",
                   "who wants to be my business partner?", "just saw a UFO for sale... saving up 🛸", "the moon casino is calling me 🎰",
                   "if you see me running, i am NOT robbing you 👀", "ok who keeps dancing at the plaza 😂", "i love this server ngl",
                   "taking a selfie at the tower 📸", "anyone else hear money noises?", "my tycoon is gonna be huge one day",
                   "daily gift claimed 🎁 lets gooo", "i should invest in real estate fr 🏘️", "the weather here is always perfect",
                   "racing to the green job beam, move outta the way 🏃", "GG everyone, this lobby is the best 🔥"]

CONVOS = [
    [('A', "yo {B} hows ur tycoon going"), ('B', "pretty good, just upgraded my {favB}"), ('A', "nice im saving for the quantum printer"), ('B', "good luck thats like a billion lol")],
    [('A', "{B} did u see how rich {top} is??"), ('B', "ya thats crazy"), ('A', "we should rob them fr"), ('B', "haha maybe later")],
    [('A', "what are u eating today {B}"), ('B', "{foodB} probably"), ('A', "W choice")],
    [('A', "{B} whats ur favorite building"), ('B', "{favB} for sure"), ('A', "mine is {favA}"), ('B', "respect")],
    [('A', "bro someone robbed me earlier"), ('B', "buy bodyguards lol"), ('A', "i HAVE bodyguards 😭"), ('B', "get better ones")],
    [('A', "{B} where are u from again"), ('B', "{homeB}"), ('A', "oh nice i always wanted to go there")],
    [('A', "have u met {p} yet"), ('B', "ya they seem cool"), ('A', "their tycoon is growing fast")],
    [('A', "{B} u listen to {musicA}?"), ('B', "nah im more into {musicB}"), ('A', "fair")],
]


def stylize(npc, text):
    s = npc.style
    r = random.random()
    if s == 'hype':
        if r < 0.5: text += ' ' + pick(['🔥', '🚀', '💯', '🤑', '😤'])
        if random.random() < 0.15: text = text.upper()
    elif s == 'toxic':
        text = text.lower()
        if r < 0.3: text += pick([' lol', ' bro', ' 💀', ' ez'])
    elif s == 'chill':
        text = text.lower().replace('!', '')
        if r < 0.25: text = pick(['dude ', 'bro ', 'ayy ']) + text
    elif s == 'nerd':
        text = text[:1].upper() + text[1:]
        if text[-1:].isalnum(): text += '.'
    elif s == 'rich':
        if r < 0.35: text += ' ' + pick(['💎', '👑', '🛥️'])
    elif s == 'friendly':
        if r < 0.4: text += ' ' + pick(['😊', '❤️', '✨'])
    return text


class NPC:
    def __init__(self, idx, persona, plot):
        self.id = f'npc{idx}'; self.p = persona; self.name = persona['name']; self.style = persona['style']
        self.color = persona['color']; self.plot = plot
        self.x, self.z = to_world(plot, 0, 27); self.y = 0.0; self.ry = 0.0
        self.anim = 'idle'; self.act = ''; self.wave_until = 0
        self.cash = random.uniform(30, 300); self.b = [0] * 12; self.b[0] = random.randint(1, 4)
        if random.random() < 0.5: self.b[1] = random.randint(1, 2)
        self.diamond = 0; self.quantum = 0
        self.path = []; self.speed = 7.0; self.task = 'idle'; self.task_until = time.time() + random.uniform(1, 5)
        self.target_id = None; self.partner = None; self.lead = False; self.talk_started = False; self.slot = None
        self.friend = {}; self.grudge = {}; self.revenge = None; self.greeted = {}; self.next_scan = 0
        self.face_pt = None

    # ---- economy ----
    def income(self):
        return min(1e300, sum(BIZ[i][2] * self.b[i] for i in range(12)) * 3 ** self.diamond * 99999 ** self.quantum)

    def next_build(self):
        best = None
        for j in range(12):
            if j == 0 or self.b[j - 1] > 0:
                cost = BIZ[j][1] * 1.15 ** self.b[j]
                if best is None or cost < best[1]:
                    best = (j, cost)
        return best

    def buy_upgrades(self):
        dc = 2500 * 12 ** self.diamond
        if self.diamond < 40 and self.cash > dc * 2 and self.income() > 0:
            self.cash -= dc; self.diamond += 1
            if random.random() < 0.35: self.say(f"just bought Diamond Hands lvl {self.diamond} 💎", 1)
        qc = 1e9 * 1e9 ** self.quantum
        if self.quantum < 20 and self.cash > qc * 2:
            self.cash -= qc; self.quantum += 1
            self.say("I JUST GOT THE 99999X QUANTUM PRINTER 🖨️🖨️", 1)
            sys_chat(f"⚡ {self.name} bought the 99999X QUANTUM PRINTER!", 'od')

    # ---- helpers ----
    def say(self, text, delay=0.0):
        R.pending.append((time.time() + delay, self, stylize(self, text)))

    def where_plot(self):
        for i in range(NPLOTS):
            lx, lz = to_local(i, self.x, self.z)
            if abs(lx) < 23 and abs(lz) < 23:
                return i, lx, lz
        return None, 0, 0

    def go(self, x=None, z=None, plot=None, lx=0.0, lz=0.0, run=False):
        path = []
        cur, clx, clz = self.where_plot()
        if cur is not None and cur != plot:
            path.append(to_world(cur, 0, clz)); path.append(to_world(cur, 0, 27))
        if plot is not None:
            if cur == plot:
                path.append(to_world(plot, 0, clz))
            else:
                path.append(to_world(plot, 0, 27))
            path.append(to_world(plot, 0, lz)); path.append(to_world(plot, lx, lz))
        else:
            path.append((x, z))
        self.path = path; self.speed = 13.0 if run else 7.0

    def set_idle(self, now, t=2.0):
        self.task = 'idle'; self.path = []; self.anim = 'idle'; self.act = ''; self.task_until = now + t
        self.target_id = None; self.partner = None; self.lead = False; self.talk_started = False

    def face(self, x, z):
        self.ry = math.atan2(x - self.x, z - self.z)

    def wave(self, now, t=2.0):
        self.wave_until = now + t

    def cur_anim(self, now):
        if self.path:
            return 'run' if self.speed > 9 else 'walk'
        return 'wave' if now < self.wave_until else self.anim

    # ---- tasks ----
    def new_task(self, now):
        self.set_idle(now)
        nb = self.next_build()
        if nb and self.cash >= nb[1] and random.random() < 0.75:
            j = nb[0]; self.task = 'build'; self.slot = j
            self.act = '🏗️ building ' + BIZ[j][0]
            self.go(plot=self.plot, lx=COLS[j % 4], lz=ROWS[j // 4] + 4); self.task_until = now + 60
            return
        if self.revenge and now > self.revenge[1]:
            c = next((c for c in joined() if c.name == self.revenge[0]), None)
            self.revenge = None
            if c and c.cash > 50:
                self.start_rob(c, now, revenge=True); return
        players = [c for c in joined() if c.cash > 1000]
        opts = [('work', 24), ('jog', 10), ('sit', 12), ('talk', 12), ('shop', 8), ('dance', 6), ('phone', 8), ('visit', 10), ('idle', 6)]
        if players: opts.append(('rob', 5))
        r = random.uniform(0, sum(w for _, w in opts))
        for name, w in opts:
            r -= w
            if r <= 0: break
        if name == 'work':
            built = [j for j in range(12) if self.b[j] > 0]
            j = pick(built) if built else 0
            self.task = 'work'; self.slot = j; self.act = '💼 working at ' + BIZ[j][0]
            self.go(plot=self.plot, lx=COLS[j % 4], lz=ROWS[j // 4] + 3); self.task_until = now + 60
        elif name == 'jog':
            self.task = 'jog'; self.act = '🏃 jogging'
            a0 = math.atan2(self.z, self.x); d = pick([-1, 1])
            self.go(math.cos(a0) * ROAD_R, math.sin(a0) * ROAD_R, run=True)
            self.path += [(math.cos(a0 + d * k * 0.35) * ROAD_R, math.sin(a0 + d * k * 0.35) * ROAD_R) for k in range(1, random.randint(8, 18))]
            self.task_until = now + 90
        elif name == 'sit':
            bx, bz = pick(BENCHES); self.task = 'sit'; self.act = '🪑 chilling on a bench'; self.go(bx, bz); self.face_pt = (0, 0); self.task_until = now + 90
        elif name == 'shop':
            self.task = 'shop'; self.act = '🥤 buying a smoothie'; self.go(*KIOSK); self.face_pt = (KIOSK[0] * 1.3, KIOSK[1] * 1.3); self.task_until = now + 90
        elif name == 'dance':
            a = random.uniform(0, 6.28); r2 = random.uniform(10, 20)
            self.task = 'dance'; self.act = '💃 dancing'; self.go(math.cos(a) * r2, math.sin(a) * r2); self.task_until = now + 90
        elif name == 'phone':
            self.task = 'phone'; self.act = '📱 on the phone'; self.anim = 'phone'; self.task_until = now + random.uniform(6, 12)
        elif name == 'visit':
            others = [i for i in range(NPLOTS) if i != self.plot and plot_owner(i)]
            if not others: self.task_until = now + 2; return
            i = pick(others); self.task = 'visit'; self.slot = i; self.act = '👀 visiting ' + plot_owner(i) + "'s tycoon"
            self.go(*to_world(i, random.uniform(-6, 6), 30)); self.face_pt = plot_frame(i)[:2]; self.task_until = now + 90
        elif name == 'talk':
            free = [n for n in R.npcs if n is not self and n.task in ('idle', 'phone', 'sit', 'dance', 'work') and not n.path]
            if not free: self.task_until = now + 2; return
            o = pick(free); a = random.uniform(0, 6.28); r2 = random.uniform(12, 34)
            mx, mz = math.cos(a) * r2, math.sin(a) * r2
            for n, sx in ((self, 1.6), (o, -1.6)):
                n.set_idle(now); n.task = 'talk'; n.act = '💬 talking'; n.task_until = now + 40
                n.go(mx + sx * math.cos(a + 1.57), mz + sx * math.sin(a + 1.57))
            self.partner, o.partner, self.lead = o, self, True
        elif name == 'rob':
            self.start_rob(pick(players), now)
        else:
            self.task_until = now + random.uniform(2, 5)

    def act_on(self, c, action, now):
        """Carry out an action Claude picked for this NPC."""
        p = c.name
        if action == 'follow':
            self.start_follow(c, now)
        elif action == 'come':
            self.set_idle(now); self.task = 'come'; self.target_id = c.id; self.act = '🚶 coming to ' + p
            self.task_until = now + 30; self.path = [(c.x, c.z)]; self.speed = 9.0
        elif action == 'stop':
            self.set_idle(now, 10)
        elif action == 'leave':
            self.set_idle(now); self.task = 'work'; self.slot = 0; self.act = '😒 leaving'
            self.go(plot=self.plot, lx=COLS[0], lz=ROWS[0] + 3)
        elif action == 'dance':
            self.set_idle(now); self.task = 'dance'; self.anim = 'dance'; self.act = '💃 dancing'; self.task_until = now + 10
        elif action == 'sit':
            bx, bz = min(BENCHES, key=lambda b: math.hypot(b[0] - self.x, b[1] - self.z))
            self.set_idle(now); self.task = 'sit'; self.act = '🪑 chilling on a bench'; self.go(bx, bz); self.task_until = now + 90
        elif action == 'race':
            self.set_idle(now); self.task = 'jog'; self.act = '🏃 racing ' + p; a0 = math.atan2(self.z, self.x)
            self.go(math.cos(a0) * ROAD_R, math.sin(a0) * ROAD_R, run=True)
            self.path += [(math.cos(a0 + k * 0.35) * ROAD_R, math.sin(a0 + k * 0.35) * ROAD_R) for k in range(1, 18)]
            self.task_until = now + 90
        elif action == 'wave':
            self.wave(now, 2.5); self.face(c.x, c.z)
        elif action == 'rob':
            if c.cash > 50: self.start_rob(c, now, quiet=True)
        elif action == 'gift':
            if self.cash >= 100 and self.grudge.get(p, 0) <= 0:
                amt = self.cash * random.uniform(0.02, 0.06); self.cash -= amt
                c.send({'t': 'gift', 'amount': amt, 'from': self.name})

    def start_rob(self, c, now, revenge=False, quiet=False):
        self.set_idle(now); self.task = 'rob'; self.target_id = c.id; self.act = '😈 hunting ' + c.name
        self.task_until = now + 25
        if quiet: return
        if revenge: self.say(pick([f"{c.name} remember me? 😈 im coming for ur money", f"payback time {c.name}"]))
        elif random.random() < 0.6: self.say(pick([f"{c.name} watch ur back 👀", f"im coming for u {c.name} 😈", f"{c.name} nice cash... would be a shame if someone took it"]))

    def start_follow(self, c, now):
        self.set_idle(now); self.task = 'follow'; self.target_id = c.id; self.act = '👣 following ' + c.name; self.task_until = now + 75

    def arrive(self, now):
        t = self.task
        if t == 'build':
            j = self.slot; cost = BIZ[j][1] * 1.15 ** self.b[j]
            if self.cash >= cost:
                self.cash -= cost; self.b[j] += 1
                if self.b[j] == 1 and random.random() < 0.7: self.say(pick([f"just built my first {BIZ[j][0]} 🏗️", f"{BIZ[j][0]} unlocked lets gooo", f"new {BIZ[j][0]} just dropped at my tycoon"]), 0.5)
                elif self.b[j] % 10 == 0: self.say(f"{BIZ[j][0]} is level {self.b[j]} now", 0.5)
            self.anim = 'work'; self.face(*to_world(self.plot, COLS[j % 4], ROWS[j // 4] - 2)); self.task_until = now + 2.5
        elif t == 'work':
            j = self.slot; self.anim = 'work'; self.face(*to_world(self.plot, COLS[j % 4], ROWS[j // 4] - 2)); self.task_until = now + random.uniform(8, 16)
        elif t == 'jog':
            self.task_until = now
        elif t == 'sit':
            self.anim = 'sit'; self.face(0, 0); self.ry += math.pi; self.task_until = now + random.uniform(10, 22)
            self.ry = math.atan2(-self.x, -self.z)
        elif t == 'shop':
            self.anim = 'idle'; self.face(*self.face_pt); self.task_until = now + 6
            if random.random() < 0.3: self.say(pick(["this smoothie slaps", "mango smoothie >>>", "why is a smoothie $9 bruh"]), 5)
        elif t == 'dance':
            self.anim = 'dance'; self.task_until = now + random.uniform(6, 12)
        elif t == 'visit':
            self.face(*self.face_pt); self.wave(now, 2.5); self.task_until = now + 5
            owner = plot_owner(self.slot)
            if owner and random.random() < 0.6:
                self.say(pick([f"nice tycoon {owner}! 🔥", f"{owner} ur base is looking clean", f"{owner} how did u get so many buildings", f"wow {owner}'s tycoon is huge"]), 1)
        elif t == 'talk':
            self.anim = 'talk'
        elif t == 'come':
            self.anim = 'idle'; self.wave(now, 2); self.task_until = now + 6
        elif t in ('follow', 'rob'):
            pass
        else:
            self.task_until = now + 1

    def update(self, dt, now):
        self.cash = min(1e300, self.cash + self.income() * dt)
        self.buy_upgrades()
        if self.task in ('follow', 'rob', 'come'):
            c = client_by_id(self.target_id)
            if not c or now > self.task_until:
                if c and self.task == 'rob': self.say(pick([f"ugh {c.name} is too fast", f"next time {c.name} 😤", f"{c.name} ran away lol"]))
                if c and self.task == 'follow': self.say(pick([f"ok {c.name} im gonna go do my own thing", "aight im done following lol"]))
                self.set_idle(now, 2)
            else:
                d = math.hypot(c.x - self.x, c.z - self.z)
                if self.task == 'rob':
                    if d < 2.8:
                        c.send({'t': 'robbed', 'by': self.id, 'byName': self.name, 'npc': True, 'pct': random.uniform(0.05, 0.15)})
                        self.set_idle(now, 4); self.face(c.x, c.z)
                    else:
                        self.path = [(c.x, c.z)]; self.speed = 12.5
                elif self.task == 'follow':
                    if d > 4.5:
                        self.path = [(c.x - (c.x - self.x) / d * 3, c.z - (c.z - self.z) / d * 3)]; self.speed = 13.0 if d > 12 else 8.0
                    else:
                        self.path = []; self.anim = 'idle'; self.face(c.x, c.z)
                elif self.task == 'come' and self.path:
                    if d < 3.5:
                        self.path = []; self.face(c.x, c.z); self.arrive(now); self.task = 'idle'
                    else:
                        self.path = [(c.x - (c.x - self.x) / d * 2.5, c.z - (c.z - self.z) / d * 2.5)]
        if self.task == 'talk' and self.lead:
            o = self.partner
            if not o or o.task != 'talk' or now > self.task_until:
                if o and o.task == 'talk': o.set_idle(now)
                self.set_idle(now)
            elif not self.path and not o.path and not self.talk_started:
                self.talk_started = True; self.face(o.x, o.z); o.face(self.x, self.z); self.anim = o.anim = 'talk'
                self.run_convo(o, now)
        # movement
        if self.path:
            tx, tz = self.path[0]; dx, dz = tx - self.x, tz - self.z; d = math.hypot(dx, dz); step = self.speed * dt
            if d <= step:
                self.x, self.z = tx, tz; self.path.pop(0)
                if not self.path: self.arrive(now)
            else:
                self.x += dx / d * step; self.z += dz / d * step; self.ry = math.atan2(dx, dz)
            r0 = math.hypot(self.x, self.z)
            if 0 < r0 < 7.5:
                self.x *= 7.5 / r0; self.z *= 7.5 / r0
        elif now >= self.task_until and self.task not in ('follow', 'rob'):
            if self.task == 'talk' and self.lead and not self.talk_started:
                pass
            else:
                self.new_task(now)
        # notice nearby players
        if now > self.next_scan:
            self.next_scan = now + 2
            if not self.path and self.task in ('idle', 'work', 'sit', 'shop', 'phone', 'dance', 'visit'):
                for c in joined():
                    if math.hypot(c.x - self.x, c.z - self.z) < 10 and now - self.greeted.get(c.name, 0) > 180 and random.random() < 0.35:
                        self.greeted[c.name] = now; self.wave(now, 2.5); self.face(c.x, c.z)
                        g = self.grudge.get(c.name, 0)
                        if g >= 2: self.say(pick([f"{c.name} stay away from my money 😒", f"oh look its {c.name} the thief"]), 0.4)
                        else: self.say(pick([f"hey {c.name}! 👋", f"yo {c.name}", f"sup {c.name}", f"oh hi {c.name}", f"{c.name}! whats good"]), 0.4)
                        break

    def run_convo(self, o, now):
        players = [c.name for c in joined()]
        convos = [cv for cv in CONVOS if players or not any('{p}' in l for _, l in cv)]
        cv = pick(convos); top = max(everyone(), key=lambda e: e[1])[0]
        pn = pick(players) if players else ''
        vals = dict(A=self.name, B=o.name, top=top, p=pn, favA=self.p['fav'], favB=o.p['fav'], foodB=o.p['food'],
                    homeB=o.p['home'], musicA=self.p['music'], musicB=o.p['music'])
        t = 0.8
        for who, line in cv:
            spk = self if who == 'A' else o
            R.pending.append((now + t, spk, stylize(spk, line.format(**vals))))
            t += 2.2 + len(line) * 0.04
        self.task_until = o.task_until = now + t + 2

    # ---- reactions ----
    def on_robbed(self, c, now):
        self.grudge[c.name] = self.grudge.get(c.name, 0) + 2; self.friend[c.name] = self.friend.get(c.name, 0) - 2
        self.say(pick([f"{c.name} JUST ROBBED ME 😭", f"bro {c.name} give it back!!", f"ok {c.name} its WAR now", f"{c.name} how did u get past my bodyguards??", f"{c.name} u will pay for this 😡"]), random.uniform(0.8, 2))
        if random.random() < 0.6:
            self.revenge = (c.name, now + random.uniform(8, 30))

    def brain(self, c, text, now):
        lo = text.lower(); p = c.name
        intent = None
        for k, rx in INTENTS:
            if re.search(rx, lo):
                intent = k; break
        f = self.friend.get(p, 0); g = self.grudge.get(p, 0); P = self.p
        if g >= 3 and intent not in ('thanks', 'nice') and random.random() < 0.5:
            return pick([f"im not talking to u {p}", f"{p} u robbed me. bye", "🙄", "ignored."])
        if intent == 'bot':
            return pick(["beep boop 🤖 ...jk. or am i", f"what makes u say that {p} lol", f"{p} are YOU a bot?", "im just a very dedicated tycoon player 😎"])
        if intent == 'follow':
            if g >= 2: return f"nah i dont trust u {p}"
            self.start_follow(c, now); return pick([f"ok {p} lead the way 👣", "following u!", f"where we going {p}?"])
        if intent == 'come':
            if g >= 2: return f"why so u can rob me again? no"
            self.set_idle(now); self.task = 'come'; self.target_id = c.id; self.act = '🚶 coming to ' + p; self.task_until = now + 30
            self.path = [(c.x, c.z)]; self.speed = 9.0
            return pick([f"omw {p}", "coming!", f"ok ok im coming {p}"])
        if intent == 'stop':
            self.set_idle(now, 10); return pick(["ok ok im stopping", "aight i'll stay here", "fine 😐"])
        if intent == 'leave':
            if re.search(r"\b(bye|cya|goodbye|gtg)\b", lo):
                self.wave(now, 2); return pick([f"cya {p}! 👋", "byeee", f"later {p}"])
            self.set_idle(now); self.task = 'work'; self.slot = 0; self.act = '😒 leaving'; self.go(plot=self.plot, lx=COLS[0], lz=ROWS[0] + 3)
            return pick([f"fine 😒 bye {p}", "rude.", "ok im leaving geez"])
        if intent == 'dance':
            self.set_idle(now); self.task = 'dance'; self.anim = 'dance'; self.act = '💃 dancing'; self.task_until = now + 10
            return pick(["💃 watch this", "dance battle?? 🕺", "ok but only cuz u asked"])
        if intent == 'sit':
            bx, bz = min(BENCHES, key=lambda b: math.hypot(b[0] - self.x, b[1] - self.z))
            self.set_idle(now); self.task = 'sit'; self.act = '🪑 chilling on a bench'; self.go(bx, bz); self.task_until = now + 90
            return "good idea im tired"
        if intent == 'jog':
            self.set_idle(now); self.task = 'jog'; self.act = '🏃 racing'; a0 = math.atan2(self.z, self.x)
            self.go(math.cos(a0) * ROAD_R, math.sin(a0) * ROAD_R, run=True)
            self.path += [(math.cos(a0 + k * 0.35) * ROAD_R, math.sin(a0 + k * 0.35) * ROAD_R) for k in range(1, 18)]
            self.task_until = now + 90
            return pick([f"race u {p}! 🏃", "loser buys smoothies", "ur on"])
        if intent == 'gift':
            if self.cash < 100: return "im broke too 💀"
            if g > 0: return f"after u robbed me? lmao no"
            if f >= 1.5 or random.random() < 0.3:
                amt = self.cash * random.uniform(0.02, 0.06); self.cash -= amt
                c.send({'t': 'gift', 'amount': amt, 'from': self.name}); self.friend[p] = f - 0.5
                return pick([f"fine here's ${fmt(amt)} 💸 dont spend it all", f"sent u ${fmt(amt)}, ur welcome", f"here {p}, ${fmt(amt)}. pay me back lol"])
            return pick(["nah get ur own money", "lol no", "maybe if we were friends 😏", "grind like the rest of us"])
        if intent == 'mymoney':
            return f"u got ${fmt(c.cash)} {p}" + (" ...richer than me ugh" if c.cash > self.cash else " 😏 im richer tho")
        if intent == 'money':
            return pick([f"i got ${fmt(self.cash)} rn", f"${fmt(self.cash)} and making ${fmt(self.income())}/s"])
        if intent == 'richest':
            top = sorted(everyone(), key=lambda e: -e[1])[0]
            if top[0] == self.name: return "me obviously 😎"
            if top[0] == p: return f"u are lol {p}. share some?"
            return f"{top[0]} with ${fmt(top[1])}"
        if intent == 'doing':
            return (self.act.split(' ', 1)[1] if self.act else 'just chilling') + ' rn'
        if intent == 'where':
            cur, _, _ = self.where_plot(); r = math.hypot(self.x, self.z)
            if cur == self.plot: return "at my tycoon, come visit"
            if cur is not None: return f"at {plot_owner(cur) or 'someone'}'s tycoon"
            if r < 44: return "at the plaza by the big tower"
            if abs(r - ROAD_R) < 6: return "on the road around the city"
            return "just walking around"
        if intent == 'from': return pick([f"{P['home']}", f"im from {P['home']}", f"{P['home']} 🌴" if P['home'] in ('Miami', 'California') else f"{P['home']}, wbu?"])
        if intent == 'how':
            if g > 0: return "was better before u robbed me"
            return pick(["good just grinding", "pretty good! u?", "tired but rich", "chillin", "great, my tycoon is booming"])
        if intent == 'age': return pick([f"im {P['age']}", f"{P['age']}, why", f"{P['age']} lol"])
        if intent == 'time': return f"its {datetime.datetime.now().strftime('%I:%M %p').lstrip('0')} here"
        if intent == 'food': return pick([f"im craving {P['food']} rn", f"just ate {P['food']} lol", f"{P['food']} is the best food fr"])
        if intent == 'job': return pick([f"irl im a {P['job']}" if not P['job'].startswith(('school', 'running')) else f"irl im {P['job']}", "this tycoon IS my job lol"])
        if intent == 'pet': return f"i have {P['pet']}"
        if intent == 'music': return pick([f"{P['music']} while grinding", f"i only listen to {P['music']}"])
        if intent == 'weather': return pick([f"its nice in {P['home']} rn", "weather in here is always perfect lol"])
        if intent == 'sleep': return pick(["sleep is for people without tycoons", "i should sleep but money", "5 more minutes"])
        if intent == 'tips': return pick(TIPS)
        if intent == 'rob':
            if 'me' in lo.split() or 'you' in lo or 'u' in lo.split(): return pick([f"dont even try it {p}", "my bodyguards are ready 🕴️", f"try me {p} 😤"])
            return pick(["robbing is so op in this game", "i got robbed earlier 😭", "buy defense upgrades fr"])
        if intent == 'insult':
            self.grudge[p] = g + 1; self.friend[p] = f - 1
            if g + 1 >= 2 and random.random() < 0.6 and c.cash > 50:
                self.revenge = (p, now + random.uniform(4, 12)); return pick([f"keep talking {p}, im coming for ur money 😈", "ok thats it."])
            return pick([f"ur the noob {p}", "says the guy with that tycoon 💀", "cry about it", f"{p} who asked"])
        if intent == 'nice':
            self.friend[p] = f + 1; return pick([f"thanks {p}! ❤️", "appreciate it", "ik 😎", f"ur cool too {p}"])
        if intent == 'thanks': return pick([f"np {p}", "anytime", "ofc"])
        if intent == 'greet':
            self.friend[p] = f + 0.3; self.wave(now, 2); self.face(c.x, c.z)
            return pick([f"hey {p}!", f"yo {p} 👋", f"sup {p}", "hiii", f"hello {p}"])
        if intent == 'laugh': return pick(["😂", "lmao", "LOL", "ikr"])
        if intent == 'fav': return f"my fav building is the {P['fav']}"
        if intent == 'team':
            if g >= 2: return "team with a thief? no thanks"
            self.friend[p] = f + 1
            if random.random() < 0.5: self.start_follow(c, now); return f"sure {p} lets team up 🤝 i'll follow u"
            return f"bet, we're a team now {p} 🤝"
        if intent == 'yes': return pick(["bet", "ok", "W"])
        if intent == 'no': return pick(["fair", "ok then", "why not"])
        for i, b in enumerate(BIZ):
            if b[0].lower().split()[0] in lo:
                return pick([f"{b[0]} is so good", f"i have {self.b[i]} {b[0]}s" if self.b[i] else f"i dont have a {b[0]} yet"])
        if '?' in text: return pick(["idk tbh", "maybe lol", "good question", f"ask {pick([n.name for n in R.npcs if n is not self])}", "hmm i think so", "no clue"])
        return pick(["true", "fr", "real", "lol", f"ok {p}", "W", "same", "facts", "interesting"])


def new_npcs():
    return [NPC(i, PERSONAS[i], NPC_PLOTS[i]) for i in range(len(PERSONAS))]


def plot_owner(i):
    for n in (R.npcs if R.bots else []):
        if n.plot == i: return n.name
    for c in joined():
        if c.plot == i: return c.name
    return None


def mentioned(n, lo):
    if n.name.lower() in lo: return True
    return any(re.search(r'\b' + re.escape(a) + r'\b', lo) for a in n.p['alias'])


CMD_RX = r"\b(follow|come|stop|stay|wait|dance|sit|race|go away|leave me)\b"


# ---------------- Claude-powered NPC replies ----------------
STYLE_DESC = {'hype': 'a hyped-up hustler with tons of energy who loves emojis like 🔥🚀💯',
              'rich': 'a smug rich kid who flexes wealth and acts a little fancy',
              'toxic': 'a cocky, competitive gamer kid who loves playful trash talk (never actually mean, no swearing)',
              'nerd': 'a smart physics nerd with proper grammar and dry humor',
              'chill': 'a super laid-back surfer who says "dude" and never stresses',
              'friendly': 'a kind, cheerful and helpful player'}
AI_ACTIONS = ['none', 'follow', 'come', 'stop', 'leave', 'dance', 'sit', 'race', 'wave', 'rob', 'gift']
AI_SCHEMA = {'type': 'object',
             'properties': {'say': {'type': 'string'},
                            'action': {'type': 'string', 'enum': AI_ACTIONS},
                            'feeling': {'type': 'string', 'enum': ['angry', 'annoyed', 'neutral', 'friendly', 'love']}},
             'required': ['say', 'action', 'feeling'], 'additionalProperties': False}
FEEL = {'angry': (-2, 2), 'annoyed': (-1, 1), 'neutral': (0, 0), 'friendly': (1, 0), 'love': (2, 0)}


def read_key(env, path):
    key = os.environ.get(env, '').strip()
    if not key and os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            key = f.read().strip()
    return key


def ai_setup():
    okey = read_key('OPENAI_API_KEY', OPENAI_KEY_FILE)
    if okey:   # OpenAI key present -> use OpenAI for bot chat
        AI['client'] = {'openai_key': okey}; AI['provider'] = 'openai'; AI['err'] = ''; return
    AI['provider'] = 'claude'
    if anthropic is None:
        AI['err'] = 'The anthropic package is not installed (pip install anthropic).'; return
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding='utf-8') as f:
            key = f.read().strip()
    if not key and not os.environ.get('ANTHROPIC_AUTH_TOKEN'):
        AI['err'] = 'No Claude API key. Put it in claude_api_key.txt next to server.py, then restart the server.'; return
    AI['client'] = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=40.0) if key else anthropic.AsyncAnthropic(max_retries=1, timeout=40.0)
    AI['err'] = ''


def ai_ready():
    return AI['client'] is not None and AI['enabled']


def ai_system(n):
    P = n.p
    return (f"You are {n.name}, a character in the online 3D game \"99999X Millionaire Simulator\". "
            "In the game, players build businesses on their tycoon plot, collect cash, buy upgrades like the 99999X Quantum Printer, "
            "rebirth for multipliers, and rob each other. Real players are chatting with you in the global game chat.\n\n"
            f"Your personality: {STYLE_DESC.get(n.style, 'a regular player')}. "
            f"You are {P['age']}, from {P['home']}, irl you are a {P['job']}, you have {P['pet']}, "
            f"your favorite food is {P['food']}, you listen to {P['music']}, your favorite building is the {P['fav']}.\n\n"
            "How to reply:\n"
            "- Write exactly what you type in chat, like a real player: short (usually under 15 words), casual, emojis sometimes.\n"
            "- React to what they actually said, using the real game numbers you are given. Stay in character.\n"
            "- If someone trash-talks you, clap back with playful banter. Keep it fun and kid-friendly: no swearing, no slurs, nothing hurtful or creepy.\n"
            "- If asked whether you are a bot or AI, be playful and vague; don't make a big deal of it.\n"
            "- Never share or ask for real personal info (addresses, phone numbers, passwords, real names).\n\n"
            "Pick an action your character does in the game:\n"
            "follow (follow the player around), come (walk to them), stop (stand still), leave (walk away to your tycoon), "
            "dance, sit (on a bench), race (run laps on the road), wave, "
            "rob (chase them and try to steal their cash: only if they really insulted or wronged you), "
            "gift (send them a little money: only if you like them and they asked nicely), none.\n"
            "Also say how you now feel about that player.")


def ai_context(n, c, text, addressed):
    top = sorted(everyone(), key=lambda e: -e[1])[:5]
    d = math.hypot(n.x - c.x, n.z - c.z)
    lines = '\n'.join(list(R.chat_log)[-10:])
    return (f"GAME STATE\n"
            f"- You: ${fmt(n.cash)} cash, earning ${fmt(n.income())}/s. Right now: {n.act or 'just standing around'}.\n"
            f"- {c.name}: ${fmt(c.cash)} net worth, {d:.0f} meters from you.\n"
            f"- How you feel about {c.name}: friendship {n.friend.get(c.name, 0):.0f}, grudge {n.grudge.get(c.name, 0):.0f} (grudge comes from them robbing or insulting you).\n"
            f"- Leaderboard: " + ', '.join(f"#{i + 1} {nm} ${fmt(v)}" for i, (nm, v) in enumerate(top)) + "\n\n"
            f"RECENT CHAT\n{lines}\n\n"
            f"{c.name} just said: \"{text}\"\n"
            f"({'They said your name.' if addressed else 'You are nearby, so you jump in.'}) Reply now as {n.name}.")


def openai_call(key, system, user):
    """Blocking call to OpenAI Chat Completions with a strict JSON schema (run in a thread)."""
    import urllib.request, urllib.error
    body = json.dumps({
        'model': OPENAI_MODEL,
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'npc_reply', 'strict': True, 'schema': AI_SCHEMA}},
        'max_completion_tokens': 300,
    }).encode()
    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=body, method='POST',
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read().decode('utf-8'))['error']['message']
        except Exception: msg = str(e)
        raise RuntimeError(f'OpenAI HTTP {e.code}: {msg}') from None
    msg = data['choices'][0]['message']
    if msg.get('refusal'):
        raise ValueError('refused')
    return msg['content']


async def ai_reply(n, c, text, addressed, now, room):
    key = (room.code, n.id)
    if key in AI['busy']:
        return
    AI['busy'].add(key)
    use(room)
    try:
        if AI['provider'] == 'openai':
            raw = await asyncio.to_thread(openai_call, AI['client']['openai_key'], ai_system(n), ai_context(n, c, text, addressed))
        else:
            resp = await AI['client'].beta.messages.create(
                model=AI_MODEL,
                max_tokens=2000,
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
                output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': AI_SCHEMA}},
                system=ai_system(n),
                messages=[{'role': 'user', 'content': ai_context(n, c, text, addressed)}])
            if resp.stop_reason == 'refusal':
                raise ValueError('refused')
            raw = next((b.text for b in resp.content if b.type == 'text'), '')
        use(room)   # other rooms may have run while we waited
        if n not in room.npcs: return
        data = json.loads(raw)
        say = re.sub(r'\s+', ' ', str(data.get('say', ''))).strip()[:160]
        action = data.get('action', 'none'); feeling = data.get('feeling', 'neutral')
        if not R.bots:
            return
        t = time.time()
        df, dg = FEEL.get(feeling, (0, 0))
        n.friend[c.name] = n.friend.get(c.name, 0) + df
        n.grudge[c.name] = max(0, n.grudge.get(c.name, 0) + dg)
        live = client_by_id(c.id)
        if live and action in AI_ACTIONS and action != 'none':
            n.act_on(live, action, t)
        elif live and feeling == 'angry' and n.grudge.get(c.name, 0) >= 3 and random.random() < 0.5:
            n.revenge = (c.name, t + random.uniform(5, 15))
        if say:
            R.pending.append((t + 0.3, n, say))
    except Exception as e:
        if anthropic is not None and isinstance(e, anthropic.AuthenticationError):
            AI['client'] = None; AI['err'] = 'Claude rejected the API key (check claude_api_key.txt).'
        elif anthropic is not None and isinstance(e, anthropic.RateLimitError):
            AI['err'] = 'Claude rate limit hit, using built-in replies for a moment.'
        elif 'OpenAI HTTP 401' in str(e):
            AI['client'] = None; AI['err'] = 'OpenAI rejected the API key (check openai_api_key.txt).'
        else:
            AI['err'] = f"{'OpenAI' if AI['provider'] == 'openai' else 'Claude'} error: {str(e)[:150]}"
        print('[ai]', AI['err'])
        use(room)
        rep = n.brain(c, text, time.time())   # fall back to the built-in replies
        if rep and R.bots and n in R.npcs: n.say(rep, 0.5)
    finally:
        AI['busy'].discard(key)


def npc_respond(c, text, now):
    if not R.bots:
        return
    lo = text.lower()
    addressed = [n for n in R.npcs if mentioned(n, lo)]
    near = sorted(R.npcs, key=lambda n: math.hypot(n.x - c.x, n.z - c.z))
    resp = addressed[:2]
    if not resp:
        n0 = near[0]; d = math.hypot(n0.x - c.x, n0.z - c.z)
        q = '?' in text or re.search(r"\b(hi|hello|hey|yo|anyone|guys|everyone)\b", lo)
        if (re.search(CMD_RX, lo) and d < 45) or (q and d < 40 and random.random() < 0.85) or (d < 18 and random.random() < 0.55):
            resp = [n0]
        elif random.random() < 0.3:
            resp = [pick(R.npcs)]
        if resp and not re.search(CMD_RX, lo) and random.random() < 0.2:
            other = pick([n for n in R.npcs if n not in resp]); resp.append(other)
    if ai_ready():
        for i, n in enumerate(resp[:2]):
            asyncio.get_running_loop().create_task(ai_reply(n, c, text, n in addressed, now, R))
        return
    t = 0
    for n in resp:
        rep = n.brain(c, text, now)
        if rep:
            t += random.uniform(0.8, 1.6) + len(rep) * 0.035
            n.say(rep, t)


def npc_react(kind, name):
    if not R.bots: return
    n = pick(R.npcs)
    lines = {'quantum': [f"{name} got the quantum printer?? 😱", f"bro {name} is gonna be so rich now"],
             'rebirth': [f"GG {name} on the rebirth!", f"{name} rebirthed already?? 😱"],
             'od': [f"{name} went 5X overdrive 😱", f"{name}'s printer go brrrr"],
             'top1': [f"how is {name} #1 already??", f"all hail {name} 👑", f"{name} is cheating fr 😤"]}.get(kind)
    if lines and random.random() < 0.8: n.say(pick(lines), random.uniform(1.5, 3))


# ---------------- message handling ----------------
def rooms_msg():
    rooms = sorted(ROOMS.values(), key=lambda r: (r.code != 'MAIN', -r.info()['players'], r.created))
    return {'t': 'rooms', 'list': [r.info() for r in rooms if r.public or r.code == 'MAIN']}


def on_message(c, m, now):
    t = m.get('t')
    if c.room is not None:
        use(c.room)
    if t == 'list':
        c.send(rooms_msg()); return
    if t in ('join', 'create') and not c.joined:
        cur = current_build(); theirs = str(m.get('build', 'old'))[:40]
        print(f"[page] {str(m.get('name', '?'))[:16]} is running build '{theirs}' (latest '{cur}')")
        if cur and theirs != cur:
            if theirs == 'old':   # pages from before auto-reload existed
                c.send({'t': 'kicked', 'reason': '🔄 The game was updated! Press Ctrl+F5 (or close this tab and open the game again) to load the new version.'})
            else:
                c.send({'t': 'reload'})
            return
        name = re.sub(r'[^\w\- .]', '', str(m.get('name', '')))[:16].strip() or f'Player{random.randint(100, 999)}'
        if t == 'create':
            rname = re.sub(r'[^\w\- .!?\'#]', '', str(m.get('rname', '')))[:24].strip() or f"{name}'s server"
            room = Room(new_code(), rname, owner=name, public=bool(m.get('public', True)), bots=bool(m.get('bots', True)))
            ROOMS[room.code] = room
            print(f"[room] {name} created '{rname}' ({room.code}, {'public' if room.public else 'private'})")
        else:
            code = str(m.get('room', 'MAIN')).upper().strip()[:8] or 'MAIN'
            room = ROOMS.get(code)
            if not room:
                c.send({'t': 'joinfail', 'msg': f'No server with code "{code}". Check the code and try again.'}); return
        use(room)
        ban = R.banned.get(name.lower(), 0)
        if ban > now:
            c.send({'t': 'joinfail', 'msg': '🚫 You are banned from this server.' if ban == float('inf') else '👢 You were kicked from this server. Try again in a couple minutes.'}); return
        if len(joined()) >= MAX_PLAYERS:
            c.send({'t': 'joinfail', 'msg': f'This server is full ({MAX_PLAYERS} players max).'}); return
        c.room = room
        taken = {x.name.lower() for x in joined()} | {n.name.lower() for n in R.npcs}
        base = name
        while name.lower() in taken:
            name = (base[:13] + str(random.randint(10, 99)))
        used = {x.plot for x in joined()}
        free = [p for p in PLAYER_PLOTS if p not in used]
        if not free:
            c.send({'t': 'full'}); return
        c.plot = free[0]; c.id = f'p{NEXT_ID[0]}'; NEXT_ID[0] += 1; c.name = name; c.joined = True
        col = str(m.get('color', ''))
        c.color = col if re.fullmatch(r'#[0-9a-fA-F]{6}', col) else '#00ff88'
        c.x, c.z = to_world(c.plot, 0, 27)
        c.send({'t': 'welcome', 'id': c.id, 'plot': c.plot, 'name': name,
                'room': {'code': R.code, 'name': R.name, 'owner': R.owner, 'main': R.code == 'MAIN', 'public': R.public}})
        c.send(market_init_msg())
        if is_owner(c): send_cfg(c)
        sys_chat(f"🟢 {name} joined the game!")
        n = pick(R.npcs)
        if R.bots: n.say(pick([f"welcome {name}!", f"yo {name} 👋", f"new player {name} lets gooo", f"{name} joined, hide ur money 😂"]), random.uniform(2, 4))
        print(f"[join] {name} ({c.id}) -> {R.name} [{R.code}] plot {c.plot}")
        return
    if not c.joined:
        return
    if t == 'p':
        c.x = num(m.get('x'), c.x, -500, 500); c.y = num(m.get('y'), c.y, -50, 500); c.z = num(m.get('z'), c.z, -500, 500)
        c.ry = num(m.get('r'), 0); c.a = str(m.get('a', 'idle'))[:8]; c.cash = num(m.get('c'), 0, 0)
        b = m.get('b')
        if isinstance(b, list): c.b = [int(num(v, 0, 0, 1e6)) for v in b[:12]] + [0] * max(0, 12 - len(b))
        c.od = 1 if m.get('od') else 0; c.reb = int(num(m.get('rb'), 0, 0, 1e5))
    elif t == 'chat':
        text = str(m.get('text', '')).strip()[:140]
        if not text or now - c.last_chat < 0.6: return
        c.last_chat = now
        broadcast({'t': 'chat', 'kind': 'player', 'id': c.id, 'name': c.name, 'text': text, 'ad': c.admin})
        npc_respond(c, text, now)
    elif t == 'rob':
        tid = str(m.get('target', ''))
        n = next((n for n in R.npcs if n.id == tid), None) if R.bots else None
        if n:
            amt = n.cash * random.uniform(0.10, 0.35); n.cash -= amt
            c.send({'t': 'robres', 'ok': True, 'amt': amt, 'name': n.name})
            sys_chat(f"🚨 [HEIST] {c.name} robbed {n.name} for ${fmt(amt)}!", 'heist'); n.on_robbed(c, now)
            return
        v = client_by_id(tid)
        if not v:
            c.send({'t': 'robres', 'ok': False, 'amt': 0, 'name': '?'}); return
        v.send({'t': 'robbed', 'by': c.id, 'byName': c.name, 'npc': False, 'pct': random.uniform(0.10, 0.35)})
    elif t == 'pay':
        v = client_by_id(m.get('target')); amt = num(m.get('amount'), 0, 0)
        if v and v is not c and amt > 0:
            v.send({'t': 'gift', 'amount': amt, 'from': c.name})
            c.send({'t': 'paid', 'ok': True, 'amt': amt, 'name': v.name})
        else:
            c.send({'t': 'paid', 'ok': False, 'amt': amt})
    elif t == 'robfail':
        n = next((n for n in R.npcs if n.id == m.get('target')), None)
        if n and random.random() < 0.8:
            n.grudge[c.name] = n.grudge.get(c.name, 0) + 1
            n.say(pick([f"nice try {c.name} 😂", f"{c.name} tried to rob me LOL", "my security > u", f"caught u {c.name} 🚔"]), 1)
    elif t == 'robreply':
        thief = str(m.get('thief', '')); ok = bool(m.get('ok')); amt = num(m.get('amt'), 0, 0)
        n = next((n for n in R.npcs if n.id == thief), None)
        if n:
            if ok:
                n.cash += amt; sys_chat(f"🚨 [HEIST] {n.name} robbed {c.name} for ${fmt(amt)}!", 'heist')
                n.say(pick([f"thanks for the cash {c.name} 😂", f"EZ money from {c.name}", f"{c.name} get better security lol", "yoink 💰"]), 1.2)
            else:
                n.say(pick([f"ugh {c.name} has good security", f"next time {c.name} 😤", f"how is {c.name}'s vault so strong"]), 1.2)
            return
        th = client_by_id(thief)
        if th:
            th.send({'t': 'robres', 'ok': ok, 'amt': amt, 'name': c.name})
            if ok: sys_chat(f"🚨 [HEIST] {th.name} robbed {c.name} for ${fmt(amt)}!", 'heist')
            else: sys_chat(f"🛡️ {th.name} tried to rob {c.name} but got stopped!")
    elif t == 'event':
        kind = m.get('kind')
        txt = {'quantum': f"⚡ {c.name} bought the 99999X QUANTUM PRINTER!", 'rebirth': f"♻️ {c.name} just REBIRTHED!",
               'od': f"⚡ {c.name} activated 5X OVERDRIVE!", 'top1': f"👑 {c.name} is now #1 on the leaderboard!"}.get(kind)
        if txt:
            sys_chat(txt, 'od'); npc_react(kind, c.name)
    elif t == 'admin_login':
        c.admin_tries = [x for x in c.admin_tries if now - x < 60]
        if len(c.admin_tries) >= 5:
            c.send({'t': 'admin_fail', 'msg': 'Too many tries. Wait a minute.'}); return
        c.admin_tries.append(now)
        if str(m.get('pw', '')) == ADMIN_PASSWORD:
            c.admin = True; c.send({'t': 'admin_ok'}); send_cfg(c); print(f"[admin] {c.name} is now admin")
        else:
            c.send({'t': 'admin_fail', 'msg': 'Wrong password'})
    elif t == 'admin' and (c.admin or is_owner(c)):
        admin_cmd(c, m, now)


def is_owner(c):
    return R is not None and R.owner and c.name.lower() == R.owner.lower() and R.code != 'MAIN'


OWNER_CMDS = {'kick', 'ban', 'unban', 'bots', 'resetbots', 'cfg', 'announce', 'public', 'deleteroom'}


def close_room(room, reason):
    """Kick everyone out of a player-made server and delete it (Main can never be deleted)."""
    if room is MAIN or room.code not in ROOMS: return False
    for x in [c for c in CLIENTS if c.joined and c.room is room]:
        x.send({'t': 'kicked', 'reason': reason})
        try: x.w.close()
        except Exception: pass
    del ROOMS[room.code]
    print(f"[room] deleted '{room.name}' ({room.code})")
    return True


def send_cfg(c=None):
    for x in ([c] if c else [x for x in joined() if x.admin or is_owner(x)]):
        x.send({'t': 'cfg', 'bots': R.bots, 'ai': AI['enabled'], 'ai_ready': AI['client'] is not None, 'ai_err': AI['err'],
                'provider': 'OpenAI' if AI['provider'] == 'openai' else 'Claude', 'owner': is_owner(x), 'admin': x.admin,
                'room': R.info(), 'banned': [k for k, v in R.banned.items() if v == float('inf')]})


def admin_cmd(c, m, now):
    cmd = m.get('cmd'); tgt = m.get('target')
    if not c.admin and cmd not in OWNER_CMDS:
        return
    who = ('ADMIN ' if c.admin else 'OWNER ') + c.name
    v = client_by_id(tgt) if tgt not in (None, 'all') else None
    amt = num(m.get('amount'), 0, 0)
    if cmd == 'bots':
        on = bool(m.get('on'))
        if on != R.bots:
            R.bots = on; reset_bots(R)   # switching bots on/off wipes all their progress
            sys_chat(f"🤖 {who} turned bots {'ON' if on else 'OFF'} (bot progress reset)")
        send_cfg(); return
    if cmd == 'deleteroom':
        if R is MAIN:
            c.send({'t': 'chat', 'kind': 'sys', 'text': "⚠️ The Main Server can't be deleted."}); return
        close_room(R, f'🗑️ The server "{R.name}" was deleted by {who}.'); return
    if cmd == 'closeall' and c.admin:
        n = sum(close_room(r, f'🧹 ADMIN {c.name} closed all servers except Main.') for r in list(ROOMS.values()) if r is not MAIN)
        use(MAIN) if c.room is MAIN else None
        c.send({'t': 'chat', 'kind': 'sys', 'text': f'🧹 Closed {n} server(s). Only Main is left.'}); return
    if cmd == 'resetbots':
        reset_bots(R); sys_chat(f"♻️ {who} reset all bot progress — bots start from scratch!")
        send_cfg(); return
    if cmd == 'ban' and v and v is not c:
        R.banned[v.name.lower()] = float('inf')
        v.send({'t': 'kicked', 'reason': f'🚫 You were BANNED from "{R.name}" by {who}.'}); sys_chat(f"🚫 {v.name} was banned from this server")
        try: v.w.close()
        except Exception: pass
        send_cfg(); return
    if cmd == 'unban':
        R.banned.pop(str(m.get('name', '')).lower(), None); send_cfg(); return
    if cmd == 'public':
        if R.code != 'MAIN': R.public = bool(m.get('on'))
        send_cfg(); return
    if cmd == 'ai':
        AI['enabled'] = bool(m.get('on'))
        if AI['enabled'] and AI['client'] is None: ai_setup()
        sys_chat(f"🧠 ADMIN {c.name} turned AI-powered bot chat {'ON' if AI['enabled'] else 'OFF'}")
        send_cfg(); return
    if cmd == 'cfg':
        send_cfg(c); return
    if cmd == 'give':
        for x in (joined() if tgt == 'all' else [v] if v else []):
            x.send({'t': 'gift', 'amount': amt, 'from': 'ADMIN ' + c.name})
        if tgt == 'all': sys_chat(f"💸 ADMIN {c.name} made it rain ${fmt(amt)} for everyone!", 'od')
    elif cmd == 'kick' and v and v is not c:
        R.banned[v.name.lower()] = now + 120
        v.send({'t': 'kicked', 'reason': f'You were kicked by {who}.'}); sys_chat(f"👢 {v.name} was kicked")
        try: v.w.close()
        except Exception: pass
    elif cmd == 'freeze' and v:
        v.send({'t': 'freeze', 'on': bool(m.get('on'))})
    elif cmd == 'bring' and v:
        v.send({'t': 'tp', 'x': c.x + 3, 'z': c.z})
    elif cmd == 'announce':
        text = str(m.get('text', '')).strip()[:120]
        if text: broadcast({'t': 'announce', 'text': text, 'name': c.name})
    elif cmd == 'odall':
        broadcast({'t': 'od'}); sys_chat(f"⚡ ADMIN {c.name} gave EVERYONE 5X OVERDRIVE!", 'od')
    elif cmd == 'npc':
        act = m.get('action')
        for n in R.npcs:
            if act == 'dance':
                n.set_idle(now); n.task = 'dance'; n.anim = 'dance'; n.act = '💃 dancing'; n.task_until = now + 15
            elif act == 'come':
                n.set_idle(now); n.task = 'come'; n.target_id = c.id; n.act = '🚶 coming to ' + c.name; n.task_until = now + 40
                n.path = [(c.x, c.z)]; n.speed = 13.0
            elif act == 'rich':
                n.cash = min(1e300, n.cash * 1000 + 1e6)
            elif act == 'broke':
                n.cash = 0
            elif act == 'jog':
                n.set_idle(now); n.task_until = 0; n.task = 'idle'
        if act == 'come': pick(R.npcs).say(f"the admin is calling us 😳", 1)
        if act == 'broke': pick(R.npcs).say("WHERE DID MY MONEY GO 😭", 1.5)
        if act == 'rich': pick(R.npcs).say("WE'RE RICH?!?! 🤑", 1.5)
    elif cmd == 'npcsay':
        n = next((n for n in R.npcs if n.id == m.get('npc')), None)
        text = str(m.get('text', '')).strip()[:140]
        if n and text: R.pending.append((now, n, text))


# ---------------- game loop / snapshot ----------------
def snapshot(now):
    P = [{'id': c.id, 'n': c.name, 'x': round(c.x, 2), 'y': round(c.y, 2), 'z': round(c.z, 2), 'r': round(c.ry, 2), 'a': c.a,
          'c': c.cash, 'b': c.b, 'pl': c.plot, 'ad': c.admin, 'col': c.color, 'od': c.od} for c in joined()]
    N = [{'id': n.id, 'n': n.name, 'x': round(n.x, 2), 'y': 0, 'z': round(n.z, 2), 'r': round(n.ry, 2), 'a': n.cur_anim(now),
          'c': n.cash, 'b': n.b, 'pl': n.plot, 'col': n.color, 'act': n.act, 'npc': 1} for n in R.npcs] if R.bots else []
    return {'t': 'snap', 'p': P, 'n': N}


def save_npcs():   # only the Main server's bots are saved to disk
    if MAIN is None: return
    try:
        data = [{'name': n.name, 'cash': n.cash, 'b': n.b, 'diamond': n.diamond, 'quantum': n.quantum, 'grudge': n.grudge, 'friend': n.friend} for n in MAIN.npcs]
        with open(SAVE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception as e:
        print('save failed:', e)


def load_npcs():
    try:
        with open(SAVE_FILE, encoding='utf-8') as f:
            data = {d['name']: d for d in json.load(f)}
        for n in MAIN.npcs:
            d = data.get(n.name)
            if d:
                n.cash = num(d.get('cash'), n.cash, 0); n.b = [int(num(v, 0, 0, 1e6)) for v in d.get('b', n.b)][:12]
                n.diamond = int(num(d.get('diamond'), 0, 0, 40)); n.quantum = int(num(d.get('quantum'), 0, 0, 20))
                n.grudge = d.get('grudge', {}); n.friend = d.get('friend', {})
        print('Loaded NPC progress from npc_save.json')
    except FileNotFoundError:
        pass
    except Exception as e:
        print('load failed:', e)


async def game_loop():
    last = time.time(); save_t = last; mkt_t = last
    while True:
        await asyncio.sleep(TICK)
        now = time.time(); dt = min(now - last, 0.5); last = now
        tick_mkt = now - mkt_t >= 1
        if tick_mkt:
            mkt_t = now; market_tick()
            mkt = {'t': 'mkt', 'p': {k: round(m['p'], 6) for k, m in MK.items()}}
        for room in list(ROOMS.values()):
            use(room)
            players = joined()
            # delete empty player-made servers after 2 minutes
            if players: room.empty_since = now
            elif room is not MAIN and now - room.empty_since > 120:
                print(f"[room] closed empty server '{room.name}' ({room.code})"); del ROOMS[room.code]; continue
            if not players and room is not MAIN:
                continue
            if tick_mkt and players: broadcast(mkt)
            if now > room.news_t:
                room.news_t = now + random.uniform(30, 70)
                if players: market_news()
            for n in (R.npcs if R.bots else []):
                try:
                    n.update(dt, now)
                except Exception as e:
                    print('npc error', n.name, e); n.set_idle(now)
            due = [p for p in R.pending if p[0] <= now]
            for p in due:
                R.pending.remove(p)
                broadcast({'t': 'chat', 'kind': 'npc', 'id': p[1].id, 'name': p[1].name, 'text': p[2]})
            if now > room.chat_t:
                room.chat_t = now + random.uniform(14, 30)
                if players and R.bots: pick(R.npcs).say(pick(GENERIC_CHATTER))
            if players:
                data = frame(json.dumps(snapshot(now), separators=(',', ':')))
                for c in players:
                    c.raw(data)
        if now - save_t > 30:
            save_t = now; save_npcs()


# ---------------- HTTP + connection handling ----------------
async def serve_http(writer, path):
    path = path.split('?')[0]
    f = FILES.get(path)
    if f and os.path.exists(os.path.join(ROOT, f[0])):
        with open(os.path.join(ROOT, f[0]), 'rb') as fh:
            data = fh.read()
        head = f'HTTP/1.1 200 OK\r\nContent-Type: {f[1]}\r\nContent-Length: {len(data)}\r\nCache-Control: no-store, must-revalidate\r\nConnection: close\r\n\r\n'
    else:
        data = b'Not found'
        head = f'HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'
    writer.write(head.encode() + data)
    await writer.drain()


async def handle(reader, writer):
    c = None
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
        lines = head.decode('latin-1').split('\r\n')
        parts = lines[0].split(' ')
        if len(parts) < 2: return
        hdr = {}
        for l in lines[1:]:
            if ':' in l:
                k, v = l.split(':', 1); hdr[k.strip().lower()] = v.strip()
        if 'websocket' in hdr.get('upgrade', '').lower() and 'sec-websocket-key' in hdr:
            acc = base64.b64encode(hashlib.sha1((hdr['sec-websocket-key'] + GUID).encode()).digest()).decode()
            writer.write(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          f'Sec-WebSocket-Accept: {acc}\r\n\r\n').encode())
            await writer.drain()
            c = Client(writer); CLIENTS.append(c)
            while True:
                op, data = await asyncio.wait_for(ws_read(reader), 180)
                if op == 0x8: break
                if op == 0x9: c.raw(frame(data, 0xA)); continue
                if op == 0x1:
                    try:
                        m = json.loads(data.decode('utf-8'))
                    except Exception:
                        continue
                    if isinstance(m, dict):
                        try:
                            on_message(c, m, time.time())
                        except Exception as e:
                            print('message error:', e)
        else:
            await serve_http(writer, parts[1])
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, asyncio.LimitOverrunError, OSError):
        pass
    except Exception as e:
        print('connection error:', e)
    finally:
        if c:
            if c in CLIENTS: CLIENTS.remove(c)
            if c.joined and c.room is not None:
                use(c.room)
                sys_chat(f"🔴 {c.name} left the game"); print(f"[leave] {c.name} ({c.room.code})")
                for n in R.npcs:
                    if n.target_id == c.id: n.set_idle(time.time())
        try:
            writer.close()
        except Exception:
            pass


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('10.255.255.255', 1)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return None


async def main():
    global MAIN
    MAIN = Room('MAIN', '🌍 Main Server'); ROOMS['MAIN'] = MAIN; use(MAIN)
    load_npcs(); ai_setup()
    server = await asyncio.start_server(handle, '0.0.0.0', PORT)
    ip = lan_ip()
    print('=' * 60)
    print('  99999X MILLIONAIRE SIMULATOR server is running!')
    print('  AI bot chat:              ' + ((f'ON (OpenAI {OPENAI_MODEL})' if AI['provider'] == 'openai' else f'ON (Claude {AI_MODEL})') if AI['client'] else 'OFF - ' + AI['err']))
    print(f'  Admin password (T menu):  {ADMIN_PASSWORD}   (change it in admin_password.txt)')
    print(f'  You:                      http://localhost:{PORT}')
    if ip: print(f'  Friends on your Wi-Fi:    http://{ip}:{PORT}')
    print('  Press Ctrl+C to stop.')
    print('=' * 60)
    asyncio.create_task(game_loop())
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        save_npcs(); print('Server stopped.')
