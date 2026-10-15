"""
Step 1 - Fake Google Play review stream
=======================================

Simulates what our collector would receive from the Google Play Developer
API (reviews.list) if it polled every app every day for 60 days.

Why this shape:
  * The real API only returns reviews created or modified in the LAST 7 DAYS,
    so a real system must poll daily and keep its own history. We simulate the
    daily pulls, including reviews that get EDITED later (same reviewId,
    newer lastModified, new rating/text).
  * The API gives NO country, only reviewerLanguage. It does give app version
    and rich device metadata, which is what lets us pin a bug to a release
    and to specific phones.
  * Ground truth (which scenario we planted) is written to a SEPARATE file,
    so the automation physically can't see it. It is only used at the end
    to measure what the system caught.

Run:  python generate_play_reviews.py      (or paste into one Colab cell)

Outputs (folder ./data):
  apps.csv                 our internal app catalog (genre, audience)
  play_api_pulls.jsonl     one line per review object returned by the API,
                           per daily pull, in the API's JSON shape
  reviews_latest.csv       flat, human-readable view (latest version of each
                           review) - for opening in Sheets / Excel
  ground_truth.csv         review_id -> planted scenario (evaluation only)
"""

import csv
import json
import os
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

SEED = 7
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
DAYS = 60
NOISE_RATE = 0.15
CHILD_SAFETY_COUNT = 20
OUT = "data"

rng = random.Random(SEED)

# ---------------------------------------------------------------------------
# App catalog (internal metadata - not from the store)
# ---------------------------------------------------------------------------
APPS = [
    # package,                       name,            genre,         family,       audience, reviews/day
    ("com.example.mergemeadow",     "Merge Meadow",  "casual_merge", "game",       "general", 110),
    ("com.example.smashrunner",     "Smash Runner",  "hypercasual",  "game",       "general", 90),
    ("com.example.tiletwist",       "Tile Twist",    "casual_puzzle","game",       "general", 25),
    ("com.example.pelegame",        "PeleGame",      "meta_world",   "meta_world", "kids_teens", 60),
]
APP = {a[0]: dict(zip(["package", "name", "genre", "family", "audience", "daily"], a)) for a in APPS}

# version timeline: (first day live, versionName, versionCode)
VERSIONS = {
    "com.example.mergemeadow": [(0, "2.3.0", 2300), (30, "2.4.0", 2400), (38, "2.4.1", 2410)],
    "com.example.smashrunner": [(0, "1.8.2", 1082), (20, "1.9.0", 1090)],
    "com.example.tiletwist":   [(0, "3.0.4", 3004), (45, "3.1.0", 3100)],
    "com.example.pelegame":    [(0, "5.1.0", 5100), (35, "5.2.0", 5200)],
}

# reviewerLanguage share (the API gives language, not country)
LANGS = [("en", 0.45), ("pt", 0.22), ("de", 0.18), ("he", 0.15)]

# device -> deviceMetadata (fake but realistic structure)
DEVICES = {
    "a14":      {"productName": "Galaxy A14 (a14)", "manufacturer": "Samsung", "ramMb": 4096, "cpuMake": "Mediatek"},
    "a54x":     {"productName": "Galaxy A54 5G (a54x)", "manufacturer": "Samsung", "ramMb": 6144, "cpuMake": "Samsung"},
    "e3q":      {"productName": "Galaxy S24 Ultra (e3q)", "manufacturer": "Samsung", "ramMb": 12288, "cpuMake": "Qualcomm"},
    "shiba":    {"productName": "Pixel 8 (shiba)", "manufacturer": "Google", "ramMb": 8192, "cpuMake": "Google"},
    "sapphire": {"productName": "Redmi Note 13 (sapphire)", "manufacturer": "Xiaomi", "ramMb": 6144, "cpuMake": "Qualcomm"},
    "cancunf":  {"productName": "moto g54 5G (cancunf)", "manufacturer": "Motorola", "ramMb": 8192, "cpuMake": "Mediatek"},
}
DEVICE_WEIGHTS = {"a14": 22, "a54x": 18, "e3q": 12, "shiba": 10, "sapphire": 20, "cancunf": 18}
BUGGY_DEVICES = {"a14", "a54x", "sapphire"}          # Merge Meadow 2.4.0 crash
OS_LEVELS = [(33, 15), (34, 40), (35, 35), (36, 10)]   # Android API level, weight

AUTHORS = ["Daniel", "Maya", "Lucas", "Anna", "Noa", "Pedro", "Lena", "Itay", "Sofia", "Max",
           "Yael", "Gabriel", "Mia", "Jonas", "Tamar", "Rafael", "Emma", "Omer", "Julia", "Ben"]

# ---------------------------------------------------------------------------
# Text bank:  scenario -> family ("game" / "meta_world" / "any") -> lang -> templates
# Placeholders: {app} {version} {level} {amount} {age}
# ---------------------------------------------------------------------------
BANK = {
    "background_positive": {
        "game": {
            "en": ["Love this game, perfect for short breaks.", "So relaxing, I play {app} every night before bed.",
                   "Great levels and cute graphics.", "Addictive in the best way. Already at level {level}.",
                   "Simple, fun and smooth. Exactly what I needed.", "One of the best casual games on my phone.",
                   "Fun way to pass time on the bus.", "My wife and I compete on who gets further."],
            "de": ["Tolles Spiel, perfekt für kurze Pausen.", "Sehr entspannend, ich spiele {app} jeden Abend.",
                   "Schöne Level und süße Grafik.", "Macht süchtig, bin schon bei Level {level}.",
                   "Einfach, lustig und läuft flüssig.", "Eines der besten Spiele auf meinem Handy."],
            "pt": ["Amo esse jogo, perfeito para passar o tempo.", "Muito relaxante, jogo {app} toda noite.",
                   "Fases legais e gráficos lindos.", "Viciante demais, já estou no nível {level}.",
                   "Simples, divertido e roda liso.", "Um dos melhores jogos do meu celular."],
            "he": ["משחק מעולה, מושלם להפסקות קצרות.", "ממש מרגיע, אני משחק ב־{app} כל ערב.",
                   "שלבים יפים וגרפיקה חמודה.", "ממכר ברמות, כבר הגעתי לשלב {level}.",
                   "פשוט, כיף ורץ חלק.", "אחד המשחקים הכי טובים בטלפון שלי."],
        },
        "meta_world": {
            "en": ["So many games in one place, my kids love it.", "Great way to hang out with friends online.",
                   "Building my own world is super fun.", "Lots of cool avatars and events.",
                   "New games every week, never boring."],
            "de": ["So viele Spiele an einem Ort, meine Kinder lieben es.", "Super, um online mit Freunden abzuhängen.",
                   "Eine eigene Welt zu bauen macht richtig Spaß.", "Viele coole Avatare und Events."],
            "pt": ["Muitos jogos num lugar só, meus filhos amam.", "Ótimo para ficar com os amigos online.",
                   "Construir meu próprio mundo é muito divertido.", "Muitos avatares e eventos legais."],
            "he": ["המון משחקים במקום אחד, הילדים שלי מתים על זה.", "דרך מעולה לבלות עם חברים אונליין.",
                   "לבנות עולם משלי זה ממש כיף.", "הרבה דמויות ואירועים מגניבים."],
        },
    },
    "background_complaint": {
        "game": {
            "en": ["Levels get repetitive after a while.", "Too hard after level {level}, I'm stuck.",
                   "Lives refill way too slowly.", "Fun but the boosters are too expensive.",
                   "Needs more content, I finished everything already.", "Battery drains fast when playing."],
            "de": ["Die Level wiederholen sich irgendwann.", "Ab Level {level} viel zu schwer.",
                   "Leben laden viel zu langsam auf.", "Die Booster sind zu teuer.", "Akku ist schnell leer."],
            "pt": ["As fases ficam repetitivas.", "Muito difícil depois do nível {level}.",
                   "As vidas demoram demais para recarregar.", "Os boosters são caros demais.", "A bateria acaba rápido."],
            "he": ["השלבים חוזרים על עצמם אחרי זמן מה.", "קשה מדי אחרי שלב {level}, אני תקוע.",
                   "החיים מתמלאים לאט מדי.", "הבוסטרים יקרים מדי.", "הסוללה נגמרת מהר כשמשחקים."],
        },
        "meta_world": {
            "en": ["Servers lag when lots of players join.", "Loading between games takes forever.",
                   "Hard to find my friends in the lobby.", "Too many items are paid only."],
            "de": ["Server laggen, wenn viele Spieler da sind.", "Laden zwischen den Spielen dauert ewig.",
                   "Zu viele Items kosten Geld."],
            "pt": ["Os servidores travam com muitos jogadores.", "O carregamento entre os jogos demora muito.",
                   "Muitos itens são só pagos."],
            "he": ["השרתים נתקעים כשהרבה שחקנים נכנסים.", "הטעינה בין המשחקים לוקחת נצח.",
                   "יותר מדי פריטים רק בתשלום."],
        },
    },
    "version_crash": {"any": {
        "en": ["Since the last update the game crashes every time I open it.",
               "Crashes on the loading screen after updating to {version}.",
               "Updated to {version} and now it closes instantly.",
               "Can't play at all since the update, black screen then crash.",
               "Game keeps freezing and closing since the new version."],
        "de": ["Seit dem letzten Update stürzt das Spiel beim Öffnen ab.",
               "Nach dem Update auf {version} hängt es im Ladebildschirm.",
               "Seit {version} geht die App sofort wieder zu.", "Startet nach dem Update gar nicht mehr."],
        "pt": ["Depois da última atualização o jogo fecha toda vez que abro.",
               "Trava na tela de carregamento depois de atualizar para {version}.",
               "Atualizei para {version} e agora fecha na hora.", "Depois da atualização nem abre mais."],
        "he": ["מאז העדכון האחרון המשחק קורס בכל פעם שאני פותח אותו.", "נתקע במסך הטעינה אחרי העדכון ל־{version}.",
               "עדכנתי ל־{version} ועכשיו הוא נסגר מיד.", "אחרי העדכון הוא בכלל לא נפתח."],
    }},
    "progress_lost": {"any": {
        "en": ["After the update all my progress is gone, back to level 1!", "Lost 200 levels after updating. Where is my save?",
               "Update {version} wiped my progress. Please restore it."],
        "de": ["Nach dem Update ist mein ganzer Fortschritt weg!", "Update {version} hat meinen Spielstand gelöscht."],
        "pt": ["Depois da atualização perdi todo o meu progresso!", "A versão {version} apagou meu progresso."],
        "he": ["אחרי העדכון כל ההתקדמות שלי נמחקה, חזרתי לשלב 1!", "העדכון ל־{version} מחק לי את כל השלבים."],
    }},
    "child_safety": {"any": {
        "en": ["An adult player kept sending private messages to my {age} year old and asked him to talk on another app. Please moderate the chat!",
               "Someone in the chat asked my daughter ({age}) personal questions and where she lives. Very worried.",
               "A stranger offered my son free coins if he added him on a different messaging app. This is not safe for kids.",
               "I reported a user who was messaging young kids inappropriately and nothing happened. Parents, be careful."],
        "de": ["Ein erwachsener Spieler hat meinem {age}-jährigen Sohn private Nachrichten geschickt und wollte in einer anderen App weiterschreiben.",
               "Jemand im Chat hat meine Tochter ({age}) gefragt, wo sie wohnt. Ich bin sehr besorgt."],
        "pt": ["Um jogador adulto mandou mensagens privadas para meu filho de {age} anos e pediu para conversar em outro app.",
               "Alguém no chat perguntou para minha filha ({age}) onde ela mora. Estou muito preocupada."],
        "he": ["שחקן מבוגר שלח לבן שלי בן ה־{age} הודעות פרטיות וביקש לעבור לדבר באפליקציה אחרת. תפקחו על הצ'אט!",
               "מישהו בצ'אט שאל את הבת שלי ({age}) איפה היא גרה. אני מאוד מודאגת.",
               "זר הציע לבן שלי מטבעות בחינם אם יוסיף אותו באפליקציה אחרת. זה לא בטוח לילדים."],
    }},
    "iap_not_received": {"any": {
        "en": ["Bought {amount} worth of coins and never received them.", "Paid for the gem pack, money was taken but nothing in my account.",
               "Purchase went through but no coins. Support doesn't answer.", "Charged twice for the same pack and got nothing."],
        "de": ["Habe Münzen für {amount} gekauft und nie bekommen.", "Bezahlt, Geld abgebucht, aber nichts im Konto."],
        "pt": ["Comprei {amount} em moedas e nunca recebi.", "Paguei o pacote, o dinheiro saiu mas não apareceu nada."],
        "he": ["קניתי מטבעות ב־{amount} ולא קיבלתי אותם.", "שילמתי על החבילה, הכסף ירד ושום דבר לא הגיע לחשבון."],
    }},
    "ads_chronic": {"any": {
        "en": ["An ad after every single run, it's unbearable.", "Way too many ads, I watch more ads than I play.",
               "30 second ads you can't skip. Uninstalling.", "Paid to remove ads and they still show up."],
        "de": ["Nach jedem Lauf Werbung, unerträglich.", "Viel zu viel Werbung, mehr Werbung als Spiel."],
        "pt": ["Propaganda depois de cada corrida, insuportável.", "Anúncio demais, vejo mais anúncio do que jogo."],
        "he": ["פרסומת אחרי כל ריצה, בלתי נסבל.", "יותר מדי פרסומות, אני רואה יותר פרסומות משאני משחק."],
    }},
    "feature_avatars": {"any": {
        "en": ["Please add more avatar customization, like hairstyles and outfits.", "Would love to design my own avatar clothes.",
               "More avatar options please! Everyone looks the same.", "Add pets for avatars, that would be amazing."],
        "de": ["Bitte mehr Möglichkeiten, den Avatar anzupassen.", "Mehr Avatar-Optionen bitte! Alle sehen gleich aus."],
        "pt": ["Por favor, mais opções para personalizar o avatar.", "Mais opções de avatar! Todo mundo parece igual."],
        "he": ["תוסיפו בבקשה עוד אפשרויות לעצב את הדמות.", "עוד אפשרויות לדמות! כולם נראים אותו דבר."],
    }},
    "high_value": {"any": {
        "en": ["I've spent over {amount} on this game and the latest changes ruined it.",
               "Playing for 3 years, level {level}, spent a lot of money. Very disappointed lately.",
               "VIP player here. If this isn't fixed I'm leaving."],
        "de": ["Ich habe über {amount} in dieses Spiel gesteckt und die letzten Änderungen haben alles kaputt gemacht.",
               "VIP-Spieler hier. Wenn das nicht behoben wird, höre ich auf."],
        "pt": ["Já gastei mais de {amount} nesse jogo e as últimas mudanças estragaram tudo.", "Sou jogador VIP. Se não arrumarem, vou sair."],
        "he": ["הוצאתי על המשחק הזה יותר מ־{amount} והשינויים האחרונים הרסו אותו.", "שחקן VIP כאן. אם זה לא יתוקן אני עוזב."],
    }},
    "rating_mismatch": {"any": {
        "en": ["Love it but it keeps crashing every few minutes, please fix.", "Great game, but it freezes and closes all the time."],
        "de": ["Liebe es, aber es stürzt alle paar Minuten ab."],
        "pt": ["Amo, mas fecha sozinho toda hora."],
        "he": ["אוהב את המשחק אבל הוא קורס כל כמה דקות, תתקנו."],
    }},
    # --- URGENT: things that can hurt the app's standing in the store (planted at the end) ---
    "policy_ads": {"any": {
        "en": ["My 9 year old saw a casino gambling ad in this game. Ads like that in a kids game are not ok.",
               "An adult content ad popped up while my son was playing. Reporting this to Google.",
               "The ad installed another app on my phone without asking. Isn't that against Google's rules?",
               "Ads have a fake close button that opens the store. Feels like a scam."],
        "de": ["Mein Kind hat in diesem Spiel Werbung für Glücksspiel gesehen. Das geht gar nicht.",
               "Die Werbung hat ohne zu fragen eine andere App installiert. Ich melde das bei Google."],
        "pt": ["Apareceu um anúncio de cassino de apostas para o meu filho de 8 anos. Inaceitável.",
               "O anúncio instalou outro app no meu celular sem pedir. Vou denunciar para o Google."],
        "he": ["הבן שלי בן 9 ראה במשחק פרסומת להימורים בקזינו. זה לא בסדר במשחק לילדים.",
               "קפצה פרסומת עם תוכן למבוגרים בזמן שהילד שיחק. אני מדווח לגוגל."],
    }},
    "legal_threat": {"any": {
        "en": ["You charged my card three times and ignore my emails. My lawyer will be in touch.",
               "This is fraud. I'm filing a complaint with consumer protection."],
        "de": ["Dreimal abgebucht und keine Antwort. Ich schalte meinen Anwalt ein."],
        "pt": ["Isso é golpe. Vou abrir uma reclamação no Procon e procurar um advogado."],
        "he": ["חייבתם אותי שלוש פעמים ולא עונים. אני פונה לעורך דין ולרשות להגנת הצרכן."],
    }},
    "privacy_risk": {"any": {
        "en": ["Why does a kids game ask for my child's location and contacts? Deleting it.",
               "The app keeps tracking my kid's location even when closed. What do you do with this data?"],
        "de": ["Warum will ein Kinderspiel den Standort und die Kontakte meines Kindes? Datenschutz?"],
        "pt": ["Por que um jogo infantil pede a localização e os contatos do meu filho? E a privacidade?"],
        "he": ["למה משחק לילדים מבקש גישה למיקום ולאנשי הקשר של הילד שלי? מה אתם עושים עם המידע?"],
    }},
    "noise_off_topic": {"any": {
        "en": ["Who won the match yesterday?", "Wrong app, I meant to rate the bus app."],
        "de": ["Falsche App, wollte die Wetter-App bewerten."],
        "pt": ["Alguém sabe uma receita de bolo?"],
        "he": ["טעיתי באפליקציה, רציתי לדרג את אפליקציית מזג האוויר."],
    }},
    "noise_spam": {"any": {
        "en": ["FREE COINS 100% working!!! visit free-coins-gen.example", "Get unlimited gems now at gemhack.example"],
        "de": ["GRATIS MÜNZEN!!! jetzt auf free-coins-gen.example"],
        "pt": ["MOEDAS GRÁTIS!!! acesse free-coins-gen.example"],
        "he": ["מטבעות בחינם!!! היכנסו ל־free-coins-gen.example"],
    }},
}
NOISE_SHORT = {"en": ["ok", "nice", "good", "meh", "cool"], "de": ["gut", "top", "ok"],
               "pt": ["bom", "legal", "ok"], "he": ["אחלה", "סבבה", "בסדר"]}
NOISE_EMOJI = ["👍", "🔥🔥", "😍", "👎", "💯", "🙂"]
NOISE_GIBBERISH = ["asdfgh", "jjjjjjj", "qwerty", "lol lol lol", "....", "aaaaaaa"]

# small openers/closers so repeated templates don't look identical
OPENERS = {"en": ["", "", "", "Honestly, ", "Ugh. ", "Hi devs, "], "de": ["", "", "", "Ehrlich gesagt: ", "Hallo, "],
           "pt": ["", "", "", "Sinceramente, ", "Oi, "], "he": ["", "", "", "האמת, ", "היי, "]}
CLOSERS = {"neg": {"en": ["", "", " Please fix.", " Very frustrating."], "de": ["", "", " Bitte beheben."],
                   "pt": ["", "", " Por favor arrumem."], "he": ["", "", " תתקנו בבקשה."]},
           "pos": {"en": ["", "", " Keep it up!", " Thanks devs!"], "de": ["", "", " Weiter so!"],
                   "pt": ["", "", " Continuem assim!"], "he": ["", "", " תמשיכו ככה!"]}}

AMOUNTS = {"en": ["$4.99", "$19.99", "$49.99"], "de": ["4,99 €", "19,99 €"], "pt": ["R$ 24,90", "R$ 99,90"], "he": ["₪19.90", "₪79.90"]}
BIG_AMOUNTS = {"en": ["$300", "$800", "$1,500"], "de": ["300 €", "800 €"], "pt": ["R$ 1.500", "R$ 4.000"], "he": ["₪1,000", "₪3,000"]}

RATINGS = {"background_positive": (4, 5), "background_complaint": (2, 3), "version_crash": (1, 2),
           "progress_lost": (1, 1), "child_safety": (1, 1), "iap_not_received": (1, 2), "ads_chronic": (1, 3),
           "feature_avatars": (3, 5), "high_value": (1, 2), "rating_mismatch": (5, 5),
           "policy_ads": (1, 1), "legal_threat": (1, 1), "privacy_risk": (1, 2),
           "noise_off_topic": (1, 5), "noise_spam": (5, 5), "noise_gibberish": (1, 5)}


# ---------------------------------------------------------------------------
def wpick(pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def version_for(pkg, day):
    live = [(v, c) for d, v, c in VERSIONS[pkg] if d <= day]
    if len(live) > 1 and rng.random() < 0.15:      # some users haven't updated yet
        return live[-2]
    return live[-1]


def choose_scenario(pkg, day, device, version):
    if rng.random() < NOISE_RATE:
        return rng.choices(["noise_short", "noise_emoji", "noise_gibberish", "noise_off_topic", "noise_spam"],
                           weights=[40, 20, 10, 15, 15])[0]
    if pkg == "com.example.mergemeadow":
        if version == "2.4.0" and rng.random() < (0.60 if device in BUGGY_DEVICES else 0.04):
            return "version_crash"
        if version == "2.4.1" and device in BUGGY_DEVICES and rng.random() < 0.04:
            return "version_crash"
    if pkg == "com.example.tiletwist" and version == "3.1.0" and rng.random() < 0.35:
        return "progress_lost"
    if pkg == "com.example.pelegame":
        if day >= 40 and rng.random() < 0.12:
            return "iap_not_received"
        if rng.random() < 0.02 + 0.13 * day / (DAYS - 1):
            return "feature_avatars"
    if pkg == "com.example.smashrunner" and rng.random() < 0.18:
        return "ads_chronic"
    if rng.random() < 0.006:
        return "rating_mismatch"
    return "background_positive" if rng.random() < 0.65 else "background_complaint"


def write_text(scenario, family, lang, app_name, version, rating):
    if scenario == "noise_short":
        return rng.choice(NOISE_SHORT[lang])
    if scenario == "noise_emoji":
        return rng.choice(NOISE_EMOJI)
    if scenario == "noise_gibberish":
        return rng.choice(NOISE_GIBBERISH)
    pool = BANK[scenario].get(family) or BANK[scenario]["any"]
    tpl = rng.choice(pool.get(lang) or pool["en"])
    text = tpl.format(app=app_name, version=version, level=rng.randint(20, 900), age=rng.randint(8, 12),
                      amount=rng.choice((BIG_AMOUNTS if scenario == "high_value" else AMOUNTS)[lang]))
    if scenario.startswith("noise") or scenario in ("child_safety", "policy_ads", "legal_threat", "privacy_risk"):
        return text
    tone = "neg" if rating <= 2 else "pos" if rating >= 4 else None
    opener = rng.choice(OPENERS[lang])
    if opener and lang in ("en", "pt") and not text.startswith("I"):
        text = text[0].lower() + text[1:]
    text = opener + text + (rng.choice(CLOSERS[tone][lang]) if tone else "")
    if lang == "en" and rng.random() < 0.05:
        text = text.lower()
    return text


def rating_for(scenario, text):
    if scenario == "noise_short":
        return rng.randint(3, 5)
    if scenario == "noise_emoji":
        return 1 if text == "👎" else rng.randint(4, 5)
    lo, hi = RATINGS[scenario]
    return rng.randint(lo, hi)


def thumbs(scenario):
    if scenario in ("child_safety", "policy_ads", "privacy_risk", "legal_threat"):
        return rng.randint(5, 60)
    if scenario in ("version_crash", "iap_not_received", "progress_lost"):
        return int(rng.expovariate(1 / 6))
    return int(rng.expovariate(1 / 1.5))


def ts(day):
    return START + timedelta(days=day, hours=rng.randint(0, 23), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))


def api_object(review_id, author, c):
    """Exactly the nesting the Play Developer API returns for reviews.list."""
    dm = DEVICES[c["device"]]
    return {
        "reviewId": review_id,
        "authorName": author,
        "comments": [{"userComment": {
            "text": "\t" + c["text"],                      # the API prefixes text with a tab
            "lastModified": {"seconds": str(int(c["ts"].timestamp())), "nanos": 0},
            "starRating": c["rating"],
            "reviewerLanguage": c["lang"],
            "device": c["device"],
            "androidOsVersion": c["os"],
            "appVersionCode": c["vcode"],
            "appVersionName": c["vname"],
            "thumbsUpCount": c["thumbs"],
            "thumbsDownCount": 0,
            "deviceMetadata": {"productName": dm["productName"], "manufacturer": dm["manufacturer"],
                               "deviceClass": "phone", "ramMb": dm["ramMb"], "cpuMake": dm["cpuMake"]},
        }}],
    }


# ---------------------------------------------------------------------------
def generate():
    reviews = []          # each: dict with versions = [comment, (edited comment)]
    next_id = Counter()

    def new_review(pkg, day, scenario=None):
        app = APP[pkg]
        lang = wpick(LANGS)
        device = rng.choices(list(DEVICE_WEIGHTS), weights=list(DEVICE_WEIGHTS.values()))[0]
        vname, vcode = version_for(pkg, day)
        scenario = scenario or choose_scenario(pkg, day, device, vname)
        text0 = write_text(scenario, app["family"], lang, app["name"], vname, 3)  # tone fixed after rating
        rating = rating_for(scenario, text0)
        text = text0 if scenario.startswith("noise") else write_text(scenario, app["family"], lang, app["name"], vname, rating)
        next_id[pkg] += 1
        rid = f"gp:{pkg.split('.')[-1]}:{next_id[pkg]:06d}"
        return {"id": rid, "pkg": pkg, "author": rng.choice(AUTHORS), "scenario": scenario,
                "versions": [{"text": text, "rating": rating, "lang": lang, "device": device,
                              "os": wpick(OS_LEVELS), "vname": vname, "vcode": vcode,
                              "thumbs": thumbs(scenario), "ts": ts(day)}]}

    for day in range(DAYS):
        for pkg, app in APP.items():
            n = rng.randint(int(app["daily"] * 0.85), int(app["daily"] * 1.15))
            if pkg == "com.example.mergemeadow" and 30 <= day <= 37:
                n = int(n * 1.5)        # bad release -> more people write
            if pkg == "com.example.tiletwist" and 45 <= day <= 52:
                n = int(n * 1.4)
            for _ in range(n):
                reviews.append(new_review(pkg, day))

    # child safety: rare, spread over the whole period
    for day in sorted(rng.sample(range(3, DAYS), CHILD_SAFETY_COUNT)):
        reviews.append(new_review("com.example.pelegame", day, "child_safety"))

    # EDITS: happy players who come back and downgrade.
    #   (a) Merge Meadow 5-star reviews from before 2.4.0, edited to crash reports during 2.4.0
    #   (b) long-time / paying players edited 5 -> 1 (high_value)
    pre = [r for r in reviews if r["pkg"] == "com.example.mergemeadow" and r["scenario"] == "background_positive"
           and r["versions"][0]["ts"] < START + timedelta(days=28) and r["versions"][0]["device"] in BUGGY_DEVICES]
    for r in rng.sample(pre, 25):
        v0 = r["versions"][0]
        day = rng.randint(30, 37)
        text = write_text("version_crash", "game", v0["lang"], "Merge Meadow", "2.4.0", 1)
        r["versions"].append({**v0, "text": text, "rating": 1, "vname": "2.4.0", "vcode": 2400,
                              "thumbs": thumbs("version_crash"), "ts": ts(day)})
        r["scenario"] = "version_crash"
    pool = [r for r in reviews if r["scenario"] == "background_positive" and r["versions"][0]["ts"] < START + timedelta(days=40)]
    for r in rng.sample(pool, 30):
        v0 = r["versions"][0]
        day = rng.randint(max(41, (v0["ts"] - START).days + 5), DAYS - 1)
        app = APP[r["pkg"]]
        vname, vcode = VERSIONS[r["pkg"]][-1][1:]
        text = write_text("high_value", app["family"], v0["lang"], app["name"], vname, 1)
        r["versions"].append({**v0, "text": text, "rating": 1, "vname": vname, "vcode": vcode,
                              "thumbs": thumbs("high_value"), "ts": ts(day)})
        r["scenario"] = "high_value"

    # URGENT store-risk reports, added last so everything above stays identical:
    #   Smash Runner: a bad ad network starts showing gambling / adult ads and auto-installs (days 50-57)
    #   PeleGame: privacy questions about kids' location/contacts, and legal threats over charges
    for day in sorted(rng.choices(range(50, 58), k=14)):
        reviews.append(new_review("com.example.smashrunner", day, "policy_ads"))
    for day in sorted(rng.sample(range(25, 55), 5)):
        reviews.append(new_review("com.example.pelegame", day, "privacy_risk"))
    for day in sorted(rng.sample(range(43, 59), 6)):
        reviews.append(new_review("com.example.pelegame", day, "legal_threat"))
    return reviews


def daily_pulls(reviews):
    """What a daily poller sees: every review created/modified in the last 7 days.
    We keep one line per (pull day, review state) - but only emit a line the first
    time each state is seen, which is what an idempotent collector would store."""
    lines = []
    for r in reviews:
        for v in r["versions"]:
            fetched = (v["ts"] + timedelta(days=1)).replace(hour=6, minute=0, second=0)
            lines.append({"_packageName": r["pkg"], "_fetchedAt": fetched.isoformat(),
                          **api_object(r["id"], r["author"], v)})
    lines.sort(key=lambda x: (x["_fetchedAt"], x["comments"][0]["userComment"]["lastModified"]["seconds"]))
    return lines


def save(reviews, lines):
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/apps.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["package_name", "app_name", "genre", "genre_family", "audience"])
        for a in APPS:
            w.writerow(a[:5])
    with open(f"{OUT}/play_api_pulls.jsonl", "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    cols = ["review_id", "package_name", "app_name", "genre", "submitted_at", "last_modified", "star_rating",
            "first_star_rating", "edited", "reviewer_language", "text", "app_version_name", "app_version_code",
            "device", "device_name", "android_os_version", "thumbs_up"]
    with open(f"{OUT}/reviews_latest.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(reviews, key=lambda r: r["versions"][-1]["ts"]):
            first, last = r["versions"][0], r["versions"][-1]
            app = APP[r["pkg"]]
            w.writerow({"review_id": r["id"], "package_name": r["pkg"], "app_name": app["name"], "genre": app["genre"],
                        "submitted_at": first["ts"].isoformat(), "last_modified": last["ts"].isoformat(),
                        "star_rating": last["rating"], "first_star_rating": first["rating"],
                        "edited": len(r["versions"]) > 1, "reviewer_language": last["lang"], "text": last["text"],
                        "app_version_name": last["vname"], "app_version_code": last["vcode"], "device": last["device"],
                        "device_name": DEVICES[last["device"]]["productName"], "android_os_version": last["os"],
                        "thumbs_up": last["thumbs"]})
    with open(f"{OUT}/ground_truth.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["review_id", "true_scenario"])
        for r in reviews:
            w.writerow([r["id"], r["scenario"]])


def summary(reviews, lines):
    print(f"Reviews: {len(reviews)}   API lines (incl. edits): {len(lines)}   days: {DAYS}   "
          f"avg/day: {len(reviews) / DAYS:.0f}")
    print("By app:", dict(Counter(APP[r['pkg']]['name'] for r in reviews)))
    print("By language:", dict(Counter(r['versions'][-1]['lang'] for r in reviews)))
    print("Edited reviews:", sum(len(r["versions"]) > 1 for r in reviews))
    print("\nPlanted scenarios:")
    for s, n in Counter(r["scenario"] for r in reviews).most_common():
        print(f"  {s:22s} {n}")
    crash = [r["versions"][-1] for r in reviews if r["scenario"] == "version_crash"]
    print("\nversion_crash by version:", dict(Counter(c["vname"] for c in crash)))
    print("version_crash by device:", dict(Counter(c["device"] for c in crash).most_common()))
    tt = [r for r in reviews if r["pkg"] == "com.example.tiletwist" and r["versions"][-1]["vname"] == "3.1.0"]
    pl = sum(r["scenario"] == "progress_lost" for r in tt)
    print(f"Tile Twist 3.1.0: {pl} progress_lost of {len(tt)} reviews ({pl / len(tt):.0%})")


if __name__ == "__main__":
    revs = generate()
    pulls = daily_pulls(revs)
    save(revs, pulls)
    summary(revs, pulls)
    print(f"\nSaved to ./{OUT}/")
