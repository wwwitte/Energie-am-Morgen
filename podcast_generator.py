"""
Energie Morgen – Automatischer Podcast-Generator
-------------------------------------------------
Ablauf:
  1. Top-News aus mehreren Google News RSS-Feeds abrufen
  2. Moderations-Richtlinien aus prompt.txt laden
  3. Groq wählt die 3 spannendsten Themen und erstellt das Skript
  4. Audio via gTTS (Google Text-to-Speech, kostenlos) erzeugen
  5. MP3 + RSS-Feed in docs/ speichern (-> GitHub Pages)
"""

import datetime
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import formatdate

import feedparser
from gtts import gTTS
from groq import Groq

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GITHUB_USERNAME = os.environ["GITHUB_USERNAME"]
GITHUB_REPO_NAME = os.environ["GITHUB_REPO_NAME"]
PODCAST_TITLE = "Energie Morgen"
PODCAST_DESC = "Täglich die spannendsten News zu erneuerbaren Energien in Deutschland – kompakt und eingeordnet."
PODCAST_LANG = "de"
PROMPT_FILE = "prompt.txt"

# Alle RSS-Feeds – je Suchbegriff ein Feed
RSS_FEEDS = [
    ("erneuerbare Energien Deutschland",  "https://news.google.com/rss/search?q=erneuerbare+Energien+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Windkraft Deutschland",             "https://news.google.com/rss/search?q=Windkraft+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("PV Deutschland",                    "https://news.google.com/rss/search?q=Photovoltaik+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Solarenergie Deutschland",          "https://news.google.com/rss/search?q=Solarenergie+Deutschland&hl=de&gl=DE&ceid=DE:de"),
    ("Stromnetz Deutschland",             "https://news.google.com/rss/search?q=Stromnetz+Deutschland&hl=de&gl=DE&ceid=DE:de"),
]

MAX_PER_FEED = 3    # Artikel pro Feed
TOP_STORIES = 3     # Groq wählt diese Anzahl für das Skript

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def base_url() -> str:
    return f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO_NAME}"


def load_prompt_config() -> str:
    """Lädt die Moderations-Richtlinien aus prompt.txt."""
    path = Path(PROMPT_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt-Datei '{PROMPT_FILE}' nicht gefunden. "
            "Bitte sicherstellen, dass die Datei im Repository liegt."
        )
    config = path.read_text(encoding="utf-8").strip()
    print(f"📋 Moderations-Richtlinien geladen ({len(config.splitlines())} Zeilen).")
    return config


def fetch_all_news() -> list[dict]:
    """Holt News aus allen konfigurierten RSS-Feeds und dedupliziert nach Titel."""
    print("📰 News aus allen Feeds abrufen ...")
    seen_titles = set()
    all_articles = []

    for topic, url in RSS_FEEDS:
        feed = feedparser.parse(url)
        count = 0
        for entry in feed.entries[:MAX_PER_FEED]:
            title = entry.get("title", "").strip()
            # Duplikate überspringen (gleicher Titel aus mehreren Feeds möglich)
            title_key = title.lower()[:60]
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            all_articles.append({
                "topic":   topic,
                "title":   title,
                "summary": entry.get("summary", "")[:300],
                "source":  entry.get("source", {}).get("title", ""),
                "link":    entry.get("link", ""),
            })
            count += 1
        print(f"   [{topic}] {count} Artikel geladen.")
        time.sleep(0.3)  # kurze Pause zwischen Requests

    print(f"   Gesamt: {len(all_articles)} eindeutige Artikel aus {len(RSS_FEEDS)} Feeds.")
    if not all_articles:
        raise RuntimeError("Keine News gefunden – alle RSS-Feeds leer oder nicht erreichbar.")
    return all_articles


def generate_script(articles: list[dict], prompt_config: str) -> str:
    """Groq wählt die 3 spannendsten Themen und erstellt das Podcast-Skript."""
    print("✍️  Skript generieren (Groq wählt Top-3-Themen) ...")
    client = Groq(api_key=GROQ_API_KEY)

    datum = datetime.date.today().strftime("%d. %B %Y")

    # Alle Artikel als nummerierte Liste für Groq aufbereiten
    news_text = "\n".join(
        f"{i+1}. [Themenbereich: {a['topic']}]"
        f"{' [Quelle: ' + a['source'] + ']' if a['source'] else ''}"
        f" {a['title']}: {a['summary']}"
        for i, a in enumerate(articles)
    )

    prompt = f"""Du bist der Moderator des deutschen Nachrichten-Podcasts „{PODCAST_TITLE}".
Heute ist der {datum}.

=== MODERATIONS-RICHTLINIEN ===
{prompt_config}
=== ENDE RICHTLINIEN ===

Unten findest du {len(articles)} aktuelle News-Artikel aus verschiedenen Themenbereichen rund um erneuerbare Energien in Deutschland.

DEINE AUFGABE:
1. Wähle die {TOP_STORIES} spannendsten und relevantesten Artikel aus der Liste aus.
2. Bevorzuge dabei thematische Vielfalt – nicht zwei sehr ähnliche Meldungen.
3. Erstelle daraus ein vollständiges Podcast-Skript mit ca. 700 Wörtern (etwa 5 Minuten Sprechzeit).
4. Halte dich strikt an die Moderations-Richtlinien.
5. Nenne bei jeder Meldung die Quelle, sofern angegeben.
6. Der Text muss direkt von einer Text-to-Speech-Engine gesprochen werden können – kein Markdown, keine Formatierung, keine Aufzählungszeichen.

VERFÜGBARE ARTIKEL:
{news_text}"""

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1500,
    )
    script = response.choices[0].message.content.strip()
    print(f"   Skript generiert ({len(script.split())} Wörter).")
    return script


def generate_audio(script: str, output_path: str) -> None:
    """Wandelt das Skript per gTTS in eine MP3-Datei um."""
    print(f"🎙️  Audio generieren -> {output_path} ...")
    tts = gTTS(text=script, lang="de", slow=False)
    tts.save(output_path)
    size_kb = Path(output_path).stat().st_size // 1024
    print(f"   Audio gespeichert ({size_kb} KB).")


def update_rss_feed(
    episode_title: str,
    episode_desc: str,
    audio_filename: str,
    audio_size_bytes: int,
) -> None:
    """Fügt die neue Episode dem RSS-Feed hinzu (docs/feed.xml)."""
    print("📡 RSS-Feed aktualisieren ...")
    feed_path = Path("docs/feed.xml")
    feed_path.parent.mkdir(parents=True, exist_ok=True)

    audio_url = f"{base_url()}/episodes/{audio_filename}"
    pub_date = formatdate(localtime=False)

    if feed_path.exists():
        ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        tree = ET.parse(feed_path)
        root = tree.getroot()
        channel = root.find("channel")
    else:
        root = ET.Element("rss")
        root.set("version", "2.0")
        root.set("xmlns:itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
        channel = ET.SubElement(root, "channel")
        ET.SubElement(channel, "title").text = PODCAST_TITLE
        ET.SubElement(channel, "link").text = base_url()
        ET.SubElement(channel, "description").text = PODCAST_DESC
        ET.SubElement(channel, "language").text = PODCAST_LANG

    item = ET.Element("item")
    ET.SubElement(item, "title").text = episode_title
    ET.SubElement(item, "description").text = episode_desc
    ET.SubElement(item, "pubDate").text = pub_date
    ET.SubElement(item, "guid").text = audio_url

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", audio_url)
    enclosure.set("type", "audio/mpeg")
    enclosure.set("length", str(audio_size_bytes))

    existing_items = channel.findall("item")
    if existing_items:
        channel.insert(list(channel).index(existing_items[0]), item)
    else:
        channel.append(item)

    for old_item in channel.findall("item")[30:]:
        channel.remove(old_item)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(feed_path, encoding="unicode", xml_declaration=True)
    print(f"   feed.xml gespeichert ({len(channel.findall('item'))} Episoden).")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main() -> None:
    today = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")
    episode_title = f"{PODCAST_TITLE} – {today.strftime('%d.%m.%Y')}"

    episodes_dir = Path("docs/episodes")
    episodes_dir.mkdir(parents=True, exist_ok=True)
    audio_filename = f"{date_str}.mp3"
    audio_path = str(episodes_dir / audio_filename)
    script_path = episodes_dir / f"{date_str}.txt"

    prompt_config = load_prompt_config()
    articles = fetch_all_news()
    script = generate_script(articles, prompt_config)

    script_path.write_text(script, encoding="utf-8")
    generate_audio(script, audio_path)

    audio_size = Path(audio_path).stat().st_size
    episode_desc = f"Die wichtigsten Nachrichten zu erneuerbaren Energien vom {today.strftime('%d.%m.%Y')}."
    update_rss_feed(episode_title, episode_desc, audio_filename, audio_size)

    print("\n✅ Fertig!")
    print(f"   Richtlinien: {PROMPT_FILE}")
    print(f"   Skript : {script_path}")
    print(f"   Audio  : {audio_path}")
    print(f"   Feed   : docs/feed.xml")
    print(f"   URL    : {base_url()}/episodes/{audio_filename}")


if __name__ == "__main__":
    main()
