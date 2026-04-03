"""
Energie Morgen – Automatischer Podcast-Generator
-------------------------------------------------
Ablauf:
  1. Top-News von Google News RSS abrufen (erneuerbare Energien DE)
  2. Podcast-Skript via Google Gemini API generieren
  3. Audio via Edge-TTS (Microsoft, kostenlos) erzeugen
  4. MP3 + RSS-Feed in docs/ speichern (-> GitHub Pages)
"""

import asyncio
import datetime
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import formatdate

import feedparser
import edge_tts
import google.generativeai as genai

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]           # GitHub Secret
GITHUB_USERNAME = os.environ["GITHUB_USERNAME"]         # z. B. "maxmustermann"
GITHUB_REPO_NAME = os.environ["GITHUB_REPO_NAME"]       # z. B. "energie-morgen"
VOICE = "de-DE-KatjaNeural"                             # Deutsche TTS-Stimme
PODCAST_TITLE = "Energie Morgen"
PODCAST_DESC = "Täglich die spannendsten News zu erneuerbaren Energien in Deutschland – kompakt und eingeordnet."
PODCAST_LANG = "de"

RSS_URL = (
    "https://news.google.com/rss/search"
    "?q=erneuerbare+Energien+Deutschland"
    "&hl=de&gl=DE&ceid=DE:de"
)

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def base_url() -> str:
    return f"https://{GITHUB_USERNAME}.github.io/{GITHUB_REPO_NAME}"


def fetch_news(max_articles: int = 5) -> list[dict]:
    """Holt die aktuellen News vom Google News RSS-Feed."""
    print("📰 News abrufen ...")
    feed = feedparser.parse(RSS_URL)
    articles = []
    for entry in feed.entries[:max_articles]:
        articles.append({
            "title": entry.get("title", ""),
            "summary": entry.get("summary", "")[:300],
            "link": entry.get("link", ""),
        })
    if not articles:
        raise RuntimeError("Keine News gefunden – RSS-Feed leer oder nicht erreichbar.")
    print(f"   {len(articles)} Artikel geladen.")
    return articles


def generate_script(articles: list[dict]) -> str:
    """Erstellt ein Podcast-Skript mit Google Gemini."""
    print("✍️  Skript generieren ...")
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    datum = datetime.date.today().strftime("%d. %B %Y")
    news_text = "\n".join(
        f"- {a['title']}: {a['summary']}" for a in articles
    )

    prompt = f"""Du bist ein kompetenter, freundlicher Podcast-Moderator des deutschen Podcasts „{PODCAST_TITLE}".

Heute ist der {datum}.

Aktuelle Top-News zu erneuerbaren Energien in Deutschland:
{news_text}

Erstelle ein Podcast-Skript mit ca. 700 Wörtern (etwa 5 Minuten Sprechzeit) im Solo-Moderations-Stil.

Aufbau:
1. Kurze, einladende Begrüßung mit Datum und Hinweis auf die heutige Hauptthemen (2–3 Sätze)
2. Ausführliche Besprechung der 2–3 wichtigsten News mit Kontext und Einordnung
3. Kurzes, motivierendes Outro mit Handlungsaufruf und Verabschiedung

Regeln:
- Natürlicher, gesprochener Stil – keine Aufzählungszeichen, kein Markdown
- Keine Sonderzeichen wie #, *, _ oder andere Formatierung
- Zahlen ausschreiben (z. B. "drei" statt "3")
- Fließender Text, der direkt von einer TTS-Engine gesprochen werden kann"""

    response = model.generate_content(prompt)
    script = response.text.strip()
    print(f"   Skript generiert ({len(script.split())} Wörter).")
    return script


async def generate_audio(script: str, output_path: str) -> None:
    """Wandelt das Skript per Edge-TTS in eine MP3-Datei um."""
    print(f"🎙️  Audio generieren -> {output_path} ...")
    communicate = edge_tts.Communicate(script, VOICE)
    await communicate.save(output_path)
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

    # Vorhandenen Feed laden oder neu erstellen
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

    # Neue Episode als erstes Item einfügen
    item = ET.Element("item")
    ET.SubElement(item, "title").text = episode_title
    ET.SubElement(item, "description").text = episode_desc
    ET.SubElement(item, "pubDate").text = pub_date
    ET.SubElement(item, "guid").text = audio_url

    enclosure = ET.SubElement(item, "enclosure")
    enclosure.set("url", audio_url)
    enclosure.set("type", "audio/mpeg")
    enclosure.set("length", str(audio_size_bytes))

    # Vor dem ersten vorhandenen Item einfügen (neueste zuerst)
    existing_items = channel.findall("item")
    if existing_items:
        channel.insert(list(channel).index(existing_items[0]), item)
    else:
        channel.append(item)

    # Feed auf max. 30 Episoden begrenzen
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

    # Ausgabepfade vorbereiten
    episodes_dir = Path("docs/episodes")
    episodes_dir.mkdir(parents=True, exist_ok=True)
    audio_filename = f"{date_str}.mp3"
    audio_path = str(episodes_dir / audio_filename)
    script_path = episodes_dir / f"{date_str}.txt"

    # Pipeline
    articles = fetch_news()
    script = generate_script(articles)

    # Skript speichern (optional, für Transparenz / Archiv)
    script_path.write_text(script, encoding="utf-8")

    # Audio erzeugen
    asyncio.run(generate_audio(script, audio_path))

    # RSS-Feed aktualisieren
    audio_size = Path(audio_path).stat().st_size
    episode_desc = f"Die wichtigsten Nachrichten zu erneuerbaren Energien vom {today.strftime('%d.%m.%Y')}."
    update_rss_feed(episode_title, episode_desc, audio_filename, audio_size)

    print("\n✅ Fertig!")
    print(f"   Skript : {script_path}")
    print(f"   Audio  : {audio_path}")
    print(f"   Feed   : docs/feed.xml")
    print(f"   URL    : {base_url()}/episodes/{audio_filename}")


if __name__ == "__main__":
    main()
