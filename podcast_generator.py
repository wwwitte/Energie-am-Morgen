"""
Energie am Morgen – Automatischer Podcast-Generator
-------------------------------------------------
Ablauf:
  1. Datenbank laden (docs/memory.json) – Archiv + Sperrfrist-Logik
  2. Top-News aus mehreren Google News RSS-Feeds abrufen
  3. Moderations-Richtlinien aus prompt.txt laden
  4. Groq wählt die 3 spannendsten neuen Themen und erstellt das Skript
  5. Audio via gTTS erzeugen
  6. MP3 + RSS-Feed + Datenbank speichern (-> GitHub Pages)

Datenbank-Logik:
  - Alle je verwendeten Artikel werden dauerhaft gespeichert (vollständiges Archiv)
  - Artikel mit ähnlichem Titel werden erst nach REUSE_AFTER_DAYS Tagen wieder zugelassen
  - Ähnlichkeit wird über gemeinsame Schlüsselwörter erkannt (keine exakte Übereinstimmung nötig)
"""

import datetime
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import formatdate

import feedparser
import anthropic
import requests as req_lib

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Default: Adam
GITHUB_USERNAME = os.environ["GITHUB_USERNAME"]
GITHUB_REPO_NAME = os.environ["GITHUB_REPO_NAME"]
PODCAST_TITLE = "Energie am Morgen"
PODCAST_DESC = "Täglich die spannendsten News zu erneuerbaren Energien in Deutschland – kompakt und eingeordnet."
PODCAST_LANG = "de"
PROMPT_FILE = "prompt.txt"
MEMORY_FILE = "docs/memory.json"

REUSE_AFTER_DAYS = 30   # Nach dieser Anzahl Tage darf ein ähnliches Thema wieder gebracht werden
SIMILARITY_THRESHOLD = 3 # Mindestanzahl gemeinsamer Schlüsselwörter für "ähnliches Thema"

RSS_FEEDS = [
    ("erneuerbare Energien Deutschland",  "https://news.google.com/rss/search?q=erneuerbare+Energien+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Windkraft Deutschland",             "https://news.google.com/rss/search?q=Windkraft+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("PV Deutschland",                    "https://news.google.com/rss/search?q=Photovoltaik+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Solarenergie Deutschland",          "https://news.google.com/rss/search?q=Solarenergie+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Stromnetz Deutschland",             "https://news.google.com/rss/search?q=Stromnetz+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Energiewende Deutschland",          "https://news.google.com/rss/search?q=Energiewende+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Wärmewende Deutschland",            "https://news.google.com/rss/search?q=W%C3%A4rmewende+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Bundesministerium Wirtschaft Energie", "https://news.google.com/rss/search?q=Bundesministerium+Wirtschaft+Energie+Deutschland&hl=de&gl=DE&ceid=DE:de"),
]

MAX_PER_FEED = 3
MAX_ARTICLE_AGE_HOURS = 48    # Nur Artikel die maximal X Stunden alt sind
TOP_STORIES = 3

# ---------------------------------------------------------------------------
# Datenbank-Funktionen
# ---------------------------------------------------------------------------

def load_memory() -> dict:
    """
    Lädt die Datenbank aus docs/memory.json.

    Struktur:
    {
      "archive": [
        {
          "title": "Windpark Nordsee: Rekordleistung im März",
          "source": "Handelsblatt",
          "topic": "Windkraft Deutschland",
          "date": "2026-04-05",
          "episode": "Energie am Morgen – 05.04.2026"
        },
        ...
      ]
    }
    """
    path = Path(MEMORY_FILE)
    if not path.exists():
        print("🗄️  Keine Datenbank gefunden – starte mit leerem Archiv.")
        return {"archive": []}

    memory = json.loads(path.read_text(encoding="utf-8"))
    if "archive" not in memory:
        memory["archive"] = []

    total = len(memory["archive"])
    # Einträge der letzten 30 Tage für den Sperrfrist-Check
    cutoff = (datetime.date.today() - datetime.timedelta(days=REUSE_AFTER_DAYS)).isoformat()
    recent = sum(1 for e in memory["archive"] if e["date"] >= cutoff)
    print(f"🗄️  Datenbank geladen – {total} Artikel im Archiv, {recent} in aktiver Sperrfrist.")
    return memory


def save_memory(memory: dict) -> None:
    """Speichert die Datenbank. Das Archiv wächst dauerhaft."""
    path = Path(MEMORY_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"🗄️  Datenbank gespeichert – {len(memory['archive'])} Artikel im Archiv.")


def extract_keywords(title: str) -> set:
    """Extrahiert bedeutungstragende Wörter aus einem Titel (mind. 4 Zeichen)."""
    # Stopwörter die keine inhaltliche Bedeutung tragen
    stopwords = {
        "eine", "einem", "einer", "eines", "wird", "wurde", "werden", "haben",
        "dass", "sind", "über", "auch", "beim", "nach", "mehr", "neue", "neuen",
        "neuer", "neues", "beim", "beim", "durch", "ihre", "ihrem", "ihren",
        "ihrer", "ihres", "ihrer", "diesem", "dieser", "dieses", "diesen",
        "gibt", "soll", "kann", "noch", "aber", "oder", "sowie", "beim",
        "beim", "beim", "beim", "beim", "beim", "beim",
    }
    words = re.findall(r'\b[a-zA-ZäöüÄÖÜß]{4,}\b', title.lower())
    return {w for w in words if w not in stopwords}


def is_too_similar(title: str, memory: dict) -> tuple[bool, str]:
    """
    Prüft ob ein Artikel-Titel einem kürzlich gebrachten Artikel zu ähnlich ist.
    Gibt (True, Grund) zurück wenn gesperrt, sonst (False, "").

    Zwei Stufen:
    1. Exakter Titel-Match → immer gesperrt (innerhalb Sperrfrist)
    2. Keyword-Overlap ≥ SIMILARITY_THRESHOLD → als ähnliches Thema gesperrt
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=REUSE_AFTER_DAYS)).isoformat()
    title_clean = title.strip().lower()
    keywords_new = extract_keywords(title)

    for entry in memory["archive"]:
        # Nur Einträge innerhalb der Sperrfrist prüfen
        if entry["date"] < cutoff:
            continue

        # Stufe 1: Exakter Match (erste 80 Zeichen)
        if entry["title"].strip().lower()[:80] == title_clean[:80]:
            return True, f"Exakter Match mit '{entry['title']}' vom {entry['date']}"

        # Stufe 2: Keyword-Ähnlichkeit
        keywords_existing = extract_keywords(entry["title"])
        overlap = keywords_new & keywords_existing
        if len(overlap) >= SIMILARITY_THRESHOLD:
            return True, (
                f"Ähnliches Thema: '{entry['title']}' vom {entry['date']} "
                f"(gemeinsame Schlüsselwörter: {', '.join(sorted(overlap))})"
            )

    return False, ""


def add_to_archive(articles: list[dict], memory: dict, episode_title: str) -> dict:
    """Fügt verwendete Artikel dauerhaft zum Archiv hinzu und aktualisiert Hot-Topics."""
    today = datetime.date.today().isoformat()
    for article in articles:
        memory["archive"].append({
            "title":   article["title"].strip(),
            "source":  article.get("source", ""),
            "topic":   article.get("topic", ""),
            "url":     article.get("link", ""),
            "date":    today,
            "episode": episode_title,
        })

    # Hot-Topic-Tracking: Keywords der letzten 7 Tage zählen
    if "hot_topics" not in memory:
        memory["hot_topics"] = {}
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    # Alte Einträge bereinigen
    memory["hot_topics"] = {
        kw: count for kw, count in memory["hot_topics"].items()
        if isinstance(count, int)
    }
    for article in articles:
        for kw in extract_keywords(article["title"]):
            if len(kw) >= 5:  # Nur bedeutungsstarke Wörter
                memory["hot_topics"][kw] = memory["hot_topics"].get(kw, 0) + 1

    return memory


def get_hot_topics(memory: dict, top_n: int = 5) -> list[str]:
    """Gibt die aktuell heißesten Themen der letzten 7 Tage zurück."""
    hot = memory.get("hot_topics", {})
    sorted_topics = sorted(hot.items(), key=lambda x: x[1], reverse=True)
    return [kw for kw, count in sorted_topics[:top_n] if count >= 2]

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def base_url() -> str:
    return f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO_NAME}"


def load_prompt_config() -> str:
    path = Path(PROMPT_FILE)
    if not path.exists():
        raise FileNotFoundError(f"Prompt-Datei '{PROMPT_FILE}' nicht gefunden.")
    config = path.read_text(encoding="utf-8").strip()
    print(f"📋 Moderations-Richtlinien geladen ({len(config.splitlines())} Zeilen).")
    return config


def resolve_url(google_url: str) -> str:
    """
    Löst einen Google News Redirect-Link zur echten Original-URL auf.
    Gibt die Original-URL zurück, oder bei Fehler die Google-URL als Fallback.
    """
    try:
        r = req_lib.get(
            google_url,
            allow_redirects=True,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastBot/1.0)"},
        )
        final_url = r.url
        # Sicherheitscheck: Falls wir doch auf Google gelandet sind, Fallback
        if "google.com" in final_url:
            return google_url
        return final_url
    except Exception:
        return google_url  # Fallback: Google-URL behalten


def fetch_all_news(memory: dict) -> list[dict]:
    """Holt News aus allen RSS-Feeds, filtert gesperrte Artikel heraus."""
    print("📰 News aus allen Feeds abrufen ...")
    seen_keys = set()
    all_articles = []
    skipped = 0

    for topic, url in RSS_FEEDS:
        feed = feedparser.parse(url)
        # Nach Erscheinungsdatum sortieren (neueste zuerst)
        sorted_entries = sorted(
            feed.entries,
            key=lambda e: e.get("published_parsed") or (0,),
            reverse=True,
        )
        # Nur Artikel der letzten MAX_ARTICLE_AGE_HOURS filtern
        # Artikel ohne Datum werden immer zugelassen (kein published_parsed)
        cutoff_ts = time.time() - MAX_ARTICLE_AGE_HOURS * 3600
        fresh_entries = [
            e for e in sorted_entries
            if e.get("published_parsed") is None  # kein Datum → zulassen
            or time.mktime(e["published_parsed"]) >= cutoff_ts
        ]
        if not fresh_entries:
            # Fallback: alle Einträge nehmen wenn keine frischen gefunden
            fresh_entries = sorted_entries
        count = 0
        for entry in fresh_entries[:MAX_PER_FEED]:
            title = entry.get("title", "").strip()
            key = title.lower()[:60]

            # Duplikat innerhalb dieses Runs
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # Sperrfrist-Check
            blocked, reason = is_too_similar(title, memory)
            if blocked:
                skipped += 1
                continue

            all_articles.append({
                "topic":   topic,
                "title":   title,
                "summary": entry.get("summary", "")[:300],
                "source":  entry.get("source", {}).get("title", ""),
                "link":    resolve_url(entry.get("link", "")),
            })
            count += 1

        print(f"   [{topic}] {count} neue Artikel.")
        time.sleep(0.3)

    print(f"   Gesamt: {len(all_articles)} neue Artikel ({skipped} wegen Sperrfrist übersprungen).")

    if not all_articles:
        raise RuntimeError(
            "Keine neuen Artikel gefunden – alle Themen wurden kürzlich bereits berichtet. "
            f"Sperrfrist: {REUSE_AFTER_DAYS} Tage."
        )
    return all_articles


def generate_script(articles: list[dict], prompt_config: str, hot_topics: list = None) -> str:
    """Claude wählt die 3 spannendsten Themen und erstellt das Podcast-Skript."""
    print("✍️  Skript generieren (Claude wählt Top-3-Themen) ...")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    datum = datetime.date.today().strftime("%d.%m.%Y")
    news_text = "\n".join(
        f"{i+1}. [Themenbereich: {a['topic']}]"
        f"{' [Quelle: ' + a['source'] + ']' if a['source'] else ''}"
        f" {a['title']}: {a['summary']}"
        for i, a in enumerate(articles)
    )

    hot_topic_hint = ""
    if hot_topics:
        hot_topic_hint = f"""

HEISSE THEMEN DER LETZTEN 7 TAGE: {', '.join(hot_topics)}
Artikel zu diesen Themen sollen bevorzugt und besonders ausführlich behandelt werden."""

    prompt = f"""Du bist der Moderator des deutschen Nachrichten-Podcasts „{PODCAST_TITLE}".
Heute ist der {datum}.{hot_topic_hint}

=== MODERATIONS-RICHTLINIEN ===
{prompt_config}
=== ENDE RICHTLINIEN ===

Unten findest du {len(articles)} aktuelle News-Artikel. Alle Artikel sind neu und wurden in den letzten 30 Tagen noch nicht im Podcast erwähnt.

DEINE AUFGABE:
1. Wähle die {TOP_STORIES} spannendsten und relevantesten Artikel aus.
2. Bevorzuge thematische Vielfalt – nicht zwei sehr ähnliche Meldungen. Bei heißen Themen darf ein Thema tiefer behandelt werden.
3. Erstelle daraus ein vollständiges Podcast-Skript (Ziel: 5 Minuten, max. 10 Minuten wenn Qualität es erfordert).
4. Halte dich strikt an die Moderations-Richtlinien inkl. Break-Tags.
5. Nenne bei jeder Meldung Quelle und Datum im Format TT.MM.JJJJ.
6. Nur fließender Sprechtext – kein Markdown, keine Formatierung, keine Aufzählungszeichen.
7. Erkläre jeden Artikel ausführlich: Was passiert? Warum ist es wichtig? Was sind die Konsequenzen?

VERFÜGBARE ARTIKEL:
{news_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,  # Erhöht für längere, tiefere Skripte (max 10min)
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Vor- und Nachbemerkungen der KI entfernen (z. B. Wortzahl, Hinweise)
    # Skript beginnt immer mit "Herzlich Willkommen"
    start_marker = "Herzlich Willkommen"
    if start_marker in raw:
        raw = raw[raw.index(start_marker):]

    # Alles nach dem Outro abschneiden
    end_marker = "auf dem Laufenden!"
    if end_marker in raw:
        raw = raw[:raw.index(end_marker) + len(end_marker)]

    script = raw.strip()
    print(f"   Skript generiert ({len(script.split())} Wörter).")
    return script


def generate_audio(script: str, output_path: str) -> None:
    """Wandelt das Skript per ElevenLabs API in eine MP3-Datei um."""
    print(f"🎙️  Audio generieren via ElevenLabs -> {output_path} ...")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": script,
        "model_id": "eleven_multilingual_v2",      # Bestes Modell für Deutsch
        "speed": 1.15,                             # Sprechgeschwindigkeit: 0.7–1.2 (1.0 = normal)
        "voice_settings": {
            "stability": 0.65,         # 0.0–1.0: höher = konsistenter, weniger ausdrucksstark
            "similarity_boost": 0.8,   # 0.0–1.0: höher = näher an Originalstimme
            "style": 0.3,              # 0.0–1.0: Ausdrucksstärke / Stil
            "use_speaker_boost": True, # Klarheit der Stimme verbessern
        },
    }
    response = req_lib.post(url, json=payload, headers=headers, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(
            f"ElevenLabs API Fehler {response.status_code}: {response.text}"
        )
    with open(output_path, "wb") as f:
        f.write(response.content)
    size_kb = Path(output_path).stat().st_size // 1024
    print(f"   Sprach-Audio gespeichert ({size_kb} KB).")


def combine_with_jingle(speech_path: str, output_path: str) -> None:
    """
    Fügt Jingle am Anfang und Ende der Episode ein (ffmpeg).
    Struktur: jingle.mp3 → speech → jingle.mp3
    Falls kein Jingle vorhanden, wird speech_path einfach nach output_path kopiert.
    """
    import subprocess
    import shutil

    jingle_path = Path("jingle.mp3")

    if not jingle_path.exists():
        print("ℹ️  Kein jingle.mp3 gefunden – Episode ohne Jingle.")
        shutil.copy(speech_path, output_path)
        return

    print("🎵 Jingle einbauen (Anfang + Ende) ...")

    import imageio_ffmpeg
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()

    # Temporäre Dateiliste für ffmpeg concat
    list_path = Path(speech_path).parent / "concat_list.txt"
    list_path.write_text(
        f"file '{jingle_path.resolve()}'\n"
        f"file '{Path(speech_path).resolve()}'\n"
        f"file '{jingle_path.resolve()}'\n",
        encoding="utf-8",
    )

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",          # Kein Re-Encoding – schnell und verlustfrei
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    list_path.unlink(missing_ok=True)  # Temp-Datei aufräumen

    if result.returncode != 0:
        print(f"⚠️  ffmpeg Fehler: {result.stderr[-300:]}")
        print("   Fallback: Episode ohne Jingle.")
        shutil.copy(speech_path, output_path)
        return

    size_kb = Path(output_path).stat().st_size // 1024
    print(f"   Episode mit Jingle gespeichert ({size_kb} KB).")


# ---------------------------------------------------------------------------
# Podcast-Metadaten (einmalig anpassen)
# ---------------------------------------------------------------------------
PODCAST_AUTHOR      = "Energie am Morgen"
PODCAST_EMAIL       = ""          # Optional: deine E-Mail für Apple Podcasts
PODCAST_CATEGORY    = "News"      # iTunes-Hauptkategorie
PODCAST_EXPLICIT    = "false"


def update_rss_feed(
    episode_title: str,
    episode_desc: str,
    audio_filename: str,
    audio_size_bytes: int,
) -> None:
    """Fügt die neue Episode dem RSS-Feed hinzu – Spotify & Apple ready."""
    print("📡 RSS-Feed aktualisieren ...")
    feed_path = Path("docs/feed.xml")
    feed_path.parent.mkdir(parents=True, exist_ok=True)

    audio_url = f"{base_url()}/episodes/{audio_filename}"
    cover_url = f"{base_url()}/cover.jpg"
    pub_date  = formatdate(localtime=False)
    itunes_ns = "http://www.itunes.com/dtds/podcast-1.0.dtd"

    def itag(parent, name, text=None, **attrs):
        el = ET.SubElement(parent, f"itunes:{name}")
        if text:
            el.text = text
        for k, v in attrs.items():
            el.set(k, v)
        return el

    # Namespace immer zuerst registrieren – vor dem Parsen
    ET.register_namespace("itunes", itunes_ns)
    ET.register_namespace("podcast", "https://podcastindex.org/namespace/1.0")

    if feed_path.exists():
        try:
            tree = ET.parse(feed_path)
            root = tree.getroot()
            channel = root.find("channel")
            episode_number = len(channel.findall("item")) + 1
        except ET.ParseError as e:
            print(f"⚠️  feed.xml beschädigt ({e}) – erstelle neu.")
            feed_path.unlink()
            root = None

    if not feed_path.exists():
        root = ET.Element("rss")
        root.set("version", "2.0")
        root.set("xmlns:itunes", itunes_ns)
        channel = ET.SubElement(root, "channel")

        # Pflichtfelder
        ET.SubElement(channel, "title").text       = PODCAST_TITLE
        ET.SubElement(channel, "link").text        = base_url()
        ET.SubElement(channel, "description").text = PODCAST_DESC
        ET.SubElement(channel, "language").text    = PODCAST_LANG

        # iTunes / Spotify Metadaten
        itag(channel, "author",   PODCAST_AUTHOR)
        itag(channel, "explicit", PODCAST_EXPLICIT)
        itag(channel, "type",     "episodic")
        cat = itag(channel, "category")
        cat.set("text", PODCAST_CATEGORY)
        img = itag(channel, "image")
        img.set("href", cover_url)

        if PODCAST_EMAIL:
            owner = ET.SubElement(channel, "itunes:owner")
            ET.SubElement(owner, "itunes:name").text  = PODCAST_AUTHOR
            ET.SubElement(owner, "itunes:email").text = PODCAST_EMAIL

        # Cover auch als RSS-Standard-Bild
        image_el = ET.SubElement(channel, "image")
        ET.SubElement(image_el, "url").text   = cover_url
        ET.SubElement(image_el, "title").text = PODCAST_TITLE
        ET.SubElement(image_el, "link").text  = base_url()

        episode_number = 1

    # Episode Item
    item = ET.Element("item")
    ET.SubElement(item, "title").text       = episode_title
    ET.SubElement(item, "description").text = episode_desc
    ET.SubElement(item, "pubDate").text     = pub_date
    ET.SubElement(item, "guid").text        = audio_url
    ET.SubElement(item, "link").text        = audio_url

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url",    audio_url)
    enclosure.set("type",   "audio/mpeg")
    enclosure.set("length", str(audio_size_bytes))

    # iTunes Episode-Tags
    itag(item, "title",       episode_title)
    itag(item, "summary",     episode_desc)
    itag(item, "explicit",    PODCAST_EXPLICIT)
    itag(item, "episodeType", "full")
    itag(item, "episode",     str(episode_number))
    ep_img = itag(item, "image")
    ep_img.set("href", cover_url)

    existing_items = channel.findall("item")
    if existing_items:
        channel.insert(list(channel).index(existing_items[0]), item)
    else:
        channel.append(item)

    # Alle Episoden dauerhaft im Feed behalten – kein Limit
    # (Spotify, Apple & Co. zeigen alle Folgen an)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(feed_path, encoding="unicode", xml_declaration=True)
    print(f"   feed.xml gespeichert ({len(channel.findall('item'))} Episoden, Episode #{episode_number}).")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    today = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")
    episode_title = f"{PODCAST_TITLE} – {today.strftime('%d.%m.%Y')}"

    episodes_dir = Path("docs/episodes")
    episodes_dir.mkdir(parents=True, exist_ok=True)

    # Cover-Bild in docs/ bereitstellen (wird vom RSS-Feed referenziert)
    cover_src = Path("cover.jpg")
    cover_dst = Path("docs/cover.jpg")
    if cover_src.exists() and not cover_dst.exists():
        import shutil
        shutil.copy(cover_src, cover_dst)
        print("🖼️  Cover-Bild nach docs/ kopiert.")
    audio_filename = f"{date_str}.mp3"
    audio_path = str(episodes_dir / audio_filename)
    script_path = episodes_dir / f"{date_str}.txt"

    memory = load_memory()
    prompt_config = load_prompt_config()
    articles = fetch_all_news(memory)
    hot_topics = get_hot_topics(memory)
    if hot_topics:
        print(f"🔥 Aktuelle Hot-Topics: {', '.join(hot_topics)}")
    script = generate_script(articles, prompt_config, hot_topics)

    script_path.write_text(script, encoding="utf-8")

    # Schritt 1: Nur Sprache generieren (temporäre Datei)
    speech_path = str(episodes_dir / f"{date_str}_speech.mp3")
    generate_audio(script, speech_path)

    # Schritt 2: Jingle vorne und hinten einbauen -> finale Episode
    combine_with_jingle(speech_path, audio_path)

    # Temporäre Sprach-Datei aufräumen
    Path(speech_path).unlink(missing_ok=True)

    # Verwendete Artikel dauerhaft ins Archiv eintragen
    memory = add_to_archive(articles, memory, episode_title)
    save_memory(memory)

    audio_size = Path(audio_path).stat().st_size
    episode_desc = f"Die wichtigsten Nachrichten zu erneuerbaren Energien vom {today.strftime('%d.%m.%Y')}."
    update_rss_feed(episode_title, episode_desc, audio_filename, audio_size)

    print("\n✅ Fertig!")
    print(f"   Richtlinien : {PROMPT_FILE}")
    print(f"   Datenbank   : {MEMORY_FILE} ({len(memory['archive'])} Einträge gesamt)")
    print(f"   Modell      : claude-sonnet-4-6 + ElevenLabs")
    print(f"   Skript      : {script_path}")
    print(f"   Audio       : {audio_path}")
    print(f"   Feed        : docs/feed.xml")
    print(f"   URL         : {base_url()}/episodes/{audio_filename}")


if __name__ == "__main__":
    main()
