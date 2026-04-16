"""
Energie am Morgen – Automatischer Podcast-Generator
-------------------------------------------------
Ablauf:
  1. Datenbank laden (docs/memory.json) – Archiv + Sperrfrist-Logik
  2. Top-News aus mehreren Google News RSS-Feeds abrufen
  3. Moderations-Richtlinien aus prompt.txt laden
  4. Claude (Anthropic) wählt die 3 spannendsten neuen Themen und erstellt das Skript
  5. Faktencheck-Schleife: zweiter Claude-Call prüft Skript auf sachliche Fehler
  6. Audio via ElevenLabs erzeugen
  7. MP3 + RSS-Feed + Datenbank speichern (-> GitHub Pages)

Datenbank-Logik:
  - Alle je verwendeten Artikel werden dauerhaft gespeichert (vollständiges Archiv)
  - Artikel mit ähnlichem Titel werden erst nach REUSE_AFTER_DAYS Tagen wieder zugelassen
  - Ähnlichkeit wird über gemeinsame Schlüsselwörter erkannt (keine exakte Übereinstimmung nötig)
"""

import datetime
import sys
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from email.utils import formatdate
from calendar import timegm

import feedparser
import anthropic
import requests as req_lib

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")  # Im Test-Modus nicht benötigt
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")  # Default: Adam
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "")
GITHUB_REPO_NAME = os.environ.get("GITHUB_REPO_NAME", "")
PODCAST_TITLE = "Energie am Morgen"
PODCAST_DESC = "Täglich die spannendsten News zu erneuerbaren Energien in Deutschland – kompakt und eingeordnet. Shownotes und Disclaimer: tinyurl.com/energieammorgen"
PODCAST_LANG = "de"
PROMPT_FILE = "prompt.txt"
MEMORY_FILE = "docs/memory.json"
PODCAST_AUTHOR   = "Energie am Morgen"
PODCAST_EMAIL = os.environ.get("PODCAST_EMAIL", "")   # Für Apple Podcasts empfohlen
PODCAST_CATEGORY = "News"                    # iTunes-Hauptkategorie

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
        "neuer", "neues", "durch", "ihre", "ihrem", "ihren",
        "ihrer", "ihres", "diesem", "dieser", "dieses", "diesen",
        "gibt", "soll", "kann", "noch", "aber", "oder", "sowie",
        "sein", "seine", "seinem", "seinen", "seiner", "nicht",
        "sich", "diese", "sehr", "dann", "also", "weil", "wenn",
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

    # Hot-Topic-Tracking: Keywords aus Archiv-Einträgen der letzten 7 Tage neu berechnen
    cutoff_7d = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    hot_topics_map = {}
    for entry in memory["archive"]:
        if entry["date"] >= cutoff_7d:
            for kw in extract_keywords(entry["title"]):
                if len(kw) >= 5:  # Nur bedeutungsstarke Wörter
                    hot_topics_map[kw] = hot_topics_map.get(kw, 0) + 1
    memory["hot_topics"] = hot_topics_map

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

            # Veröffentlichungsdatum extrahieren
            pub_struct = entry.get("published_parsed")
            pub_date_str = time.strftime("%d.%m.%Y", pub_struct) if pub_struct else "Unbekannt"

            all_articles.append({
                "topic":   topic,
                "title":   title,
                "summary": entry.get("summary", "")[:300],
                "source":  entry.get("source", {}).get("title", ""),
                "date":    pub_date_str,
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


def generate_script(articles: list[dict], prompt_config: str, hot_topics: list = None) -> tuple[str, list[int], str]:
    """Claude wählt die 3 spannendsten Themen und erstellt das Podcast-Skript.
    Gibt (script, selected_indices, title_tag) zurück."""
    print("✍️  Skript generieren (Claude wählt Top-3-Themen) ...")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    datum = datetime.date.today().strftime("%d.%m.%Y")
    news_text = "\n".join(
        f"{i+1}. [Datum: {a['date']}] [Themenbereich: {a['topic']}]"
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
5. Nenne bei jeder Meldung die Quelle. Datumsangaben immer ausschreiben (z.B. "vierzehnter April zweitausendsechsundzwanzig"), NIEMALS numerisch.
6. Nur fließender Sprechtext – kein Markdown, keine Formatierung, keine Aufzählungszeichen.
7. Erkläre jeden Artikel ausführlich: Was passiert? Warum ist es wichtig? Was sind die Konsequenzen?
8. WICHTIG: Beginne deine Antwort mit genau zwei Zeilen im Format:
   AUSWAHL: X, Y, Z
   TITEL: Thema 1, Thema 2 & Thema 3
   (AUSWAHL: Artikel-Nummern. TITEL: Kurz und knackig für den Episoden-Titel, ca. 50-80 Zeichen)

   Danach folgt direkt das Skript ab "Herzlich Willkommen".

9. ZEITLICHE EINORDNUNG: Nutze das [Datum] der Meldung nur zur korrekten zeitlichen Einordnung im Sprechtext (z. B. "gestern", "am Dienstag" oder "am [Datum]"). Lies niemals die Bezeichnung "[Datum: ...]" vor. Wenn eine Meldung vom Vortag ist, stelle dies klar heraus.

VERFÜGBARE ARTIKEL:
{news_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,  # Erhöht für längere, tiefere Skripte (max 10min)
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    # Metadaten-Zeilen parsen (AUSWAHL und TITEL)
    selected_indices = []
    title_tag = ""
    lines = raw.split("\n")
    cleaned_lines = []
    
    for line in lines:
        if line.startswith("AUSWAHL:"):
            try:
                indices = [int(x.strip()) - 1 for x in line.split(":")[1].split(",")]
                selected_indices = [i for i in indices if 0 <= i < len(articles)]
                print(f"   Claude hat Artikel {[i+1 for i in selected_indices]} gewählt.")
            except (ValueError, IndexError):
                print("   ⚠️  AUSWAHL-Zeile konnte nicht geparst werden.")
        elif line.startswith("TITEL:"):
            title_tag = line.replace("TITEL:", "").strip()
            print(f"   Vorgeschlagener Titel: {title_tag}")
        else:
            cleaned_lines.append(line)
            
    raw = "\n".join(cleaned_lines).strip()

    if not selected_indices:
        print("   ⚠️  Konnte gewählte Artikel nicht ermitteln – alle werden archiviert.")

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
    return script, selected_indices, title_tag


def fact_check_script(script: str, articles: list[dict], prompt_config: str) -> str:
    """Claude prüft das generierte Skript auf sachliche Fehler im Abgleich mit den Originalmeldungen."""
    print("🔍 Skript auf Fakten prüfen (Claude liest Korrektur) ...")
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    news_text = "\n\n".join(
        f"[Quelle: {a.get('source', 'Unbekannt')}] [Datum: {a.get('date', 'Unbekannt')}]\n"
        f"Titel: {a.get('title', '')}\n"
        f"Inhalt: {a.get('summary', '')}"
        for a in articles
    )

    prompt = f"""Du bist der Korrekteur des Podcasts "{PODCAST_TITLE}".
Deine einzige Aufgabe ist eine CHIRURGISCHE FAKTENPRÜFUNG – kein Lektorat, kein Umschreiben, keine Stilverbesserungen.

=== ORIGINAL-NACHRICHTEN ===
{news_text}

=== ZU PRÜFENDES SKRIPT ===
---
{script}
---

=== STRIKTE PRÜFREGELN ===

WAS DU KORRIGIEREN DARFST (und musst):
• Zahlen, die nicht mit den Original-Nachrichten übereinstimmen → auf den korrekten Wert aus der Quelle setzen.
• Einheiten, die falsch sind (z. B. „Gigawatt" statt „Megawatt") → korrigieren.
• Datumsangaben, die falsch sind → korrigieren. Datumsangaben müssen immer ausgeschrieben sein (z. B. „fünfzehnter April zweitausendsechsundzwanzig"), niemals numerisch.
• Zahlen müssen immer ausgeschrieben sein (z. B. „drei Gigawatt" statt „3 GW") → falls numerisch, ausschreiben.
• Quellennennung, die nachweislich falsch ist → korrigieren.
• Wortlaut, der eine sachlich falsche Behauptung transportiert (z. B. „sinkt" statt „steigt") → nur den falschen Begriff ersetzen, sonst nichts anfassen.

WAS DU KEINESFALLS ÄNDERN DARFST:
• Satzstruktur, Satzbau, Reihenfolge von Sätzen oder Absätzen.
• Formulierungen, Wortwahl und Stil – auch wenn du es besser formulieren würdest.
• Einordnungspassagen, Übergänge, Metaphern, Einleitungen, Outros.
• Break-Tags (<break time="1s"/>), die bereits im Skript stehen.
• Alles, was stilistisch oder journalistisch, aber nicht faktisch falsch ist.

GOLDENE REGEL: Wenn eine Stelle keine nachweislich falsche Zahl, Einheit, kein falsches Datum und keine sachlich falsche Behauptung enthält – lass sie UNVERÄNDERT.

Gib ausschließlich das geprüfte Skript als reinen Text aus.
Keine einleitenden Sätze, keine Anmerkungen, keine Meta-Kommentare, kein Markdown.
Das erste Element muss <break time="1s"/> gefolgt von „Herzlich Willkommen" sein.
"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    
    corrected_script = response.content[0].text.strip()

    # Marker überprüfen, um reinen Sprechtext sicherzustellen
    start_marker = "Herzlich Willkommen"
    if start_marker in corrected_script:
        # Falls davor noch "Hier ist das korrigierte Skript" o.ä. steht, abschneiden
        # Den Break-Tag wollen wir aber behalten, falls er direkt davor steht. 
        idx = corrected_script.index(start_marker)
        # Suche nach <break time="1s"/> vor dem Startmarker
        break_tag = '<break time="1s"/>'
        if break_tag in corrected_script[:idx]:
            corrected_script = corrected_script[corrected_script.index(break_tag):]
        else:
            corrected_script = corrected_script[idx:]

    end_marker = "auf dem Laufenden!"
    if end_marker in corrected_script:
        pos = corrected_script.index(end_marker) + len(end_marker)
        # Nachfolgenden Break-Tag einschließen (mit Toleranz für Whitespace/Anführungszeichen)
        remaining = corrected_script[pos:pos+50]
        break_tag = '<break time="1s"/>'
        if break_tag in remaining:
            corrected_script = corrected_script[:pos + remaining.index(break_tag) + len(break_tag)]
        else:
            corrected_script = corrected_script[:pos]

    print(f"   Faktencheck abgeschlossen ({len(corrected_script.split())} Wörter).")
    return corrected_script


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
    # Retry-Logik für transiente Netzwerk-/Timeout-Fehler
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = req_lib.post(url, json=payload, headers=headers, timeout=300)
            break
        except req_lib.exceptions.Timeout:
            if attempt < max_retries:
                wait = 10 * attempt
                print(f"   ⏳ Timeout (Versuch {attempt}/{max_retries}) – warte {wait}s und versuche erneut ...")
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"ElevenLabs API Timeout nach {max_retries} Versuchen. "
                    "Das Skript ist möglicherweise zu lang für die API."
                )
        except req_lib.exceptions.ConnectionError as e:
            if attempt < max_retries:
                wait = 10 * attempt
                print(f"   🔌 Verbindungsfehler (Versuch {attempt}/{max_retries}) – warte {wait}s ...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"ElevenLabs API Verbindungsfehler: {e}")

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
    Fügt Jingle am Anfang und Ende der Episode ein.
    Verwendet ffmpeg filter_complex concat mit Re-Encoding auf 128kbps/44100Hz
    damit Jingle und Speech-Audio (ggf. unterschiedliche Formate) zuverlässig
    zusammengefügt werden. -c copy würde bei unterschiedlichen Bitrates/Sample-Rates
    zu leerer oder kaputten Audio führen.
    """
    import subprocess
    import shutil

    jingle_path = Path("jingle.mp3")

    if not jingle_path.exists():
        print("ℹ️  Kein jingle.mp3 gefunden – Episode ohne Jingle.")
        shutil.copy(speech_path, output_path)
        return

    # ffmpeg-Binary finden: System-PATH oder imageio-ffmpeg als Fallback
    ffmpeg_bin = "ffmpeg"
    if shutil.which("ffmpeg") is None:
        try:
            import imageio_ffmpeg
            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            print("   (ffmpeg via imageio-ffmpeg gefunden)")
        except ImportError:
            print("⚠️  ffmpeg nicht verfügbar – Episode ohne Jingle.")
            shutil.copy(speech_path, output_path)
            return

    print("🎵 Jingle einbauen (Anfang + Ende) ...")

    cmd = [
        ffmpeg_bin, "-y",
        "-i", str(jingle_path.resolve()),
        "-i", str(Path(speech_path).resolve()),
        "-i", str(jingle_path.resolve()),
        "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
        "-map", "[out]",
        "-ar", "44100",
        "-ab", "128k",
        "-codec:a", "libmp3lame",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"⚠️  ffmpeg Fehler:\n{result.stderr[-500:]}")
        print("   Fallback: Episode ohne Jingle.")
        shutil.copy(speech_path, output_path)
        return

    size_kb = Path(output_path).stat().st_size // 1024
    print(f"   Episode mit Jingle gespeichert ({size_kb} KB).")



# ---------------------------------------------------------------------------
# Podcast-Metadaten
# ---------------------------------------------------------------------------
PODCAST_EXPLICIT    = "false"


def rebuild_rss_feed() -> None:
    """Baut den RSS-Feed komplett neu aus allen MP3-Dateien in docs/episodes/.

    Bei jedem Run wird der Feed von Grund auf generiert, sodass er immer
    exakt die Episoden enthält, die als MP3 im Verzeichnis liegen.
    Zusatzinfos (Beschreibung) werden aus der gleichnamigen .txt-Datei gelesen,
    falls vorhanden.
    """
    print("📡 RSS-Feed neu aufbauen ...")
    episodes_dir = Path("docs/episodes")
    feed_path = Path("docs/feed.xml")
    feed_path.parent.mkdir(parents=True, exist_ok=True)

    # Alle MP3s finden und nach Datum sortieren (älteste zuerst → Episode #1)
    mp3_files = sorted(episodes_dir.glob("*.mp3"))
    if not mp3_files:
        print("   ⚠️  Keine MP3-Dateien in docs/episodes/ gefunden – Feed wird nicht erstellt.")
        return

    cover_url = f"{base_url()}/cover.jpg"
    itunes_ns = "http://www.itunes.com/dtds/podcast-1.0.dtd"

    ET.register_namespace("itunes", itunes_ns)
    ET.register_namespace("podcast", "https://podcastindex.org/namespace/1.0")

    def itag(parent, name, text=None, **attrs):
        el = ET.SubElement(parent, f"itunes:{name}")
        if text:
            el.text = text
        for k, v in attrs.items():
            el.set(k, v)
        return el

    # --- Channel aufbauen ---
    root = ET.Element("rss")
    root.set("version", "2.0")
    root.set("xmlns:itunes", itunes_ns)
    channel = ET.SubElement(root, "channel")

    ET.SubElement(channel, "title").text       = PODCAST_TITLE
    ET.SubElement(channel, "link").text        = base_url()
    ET.SubElement(channel, "description").text = PODCAST_DESC
    ET.SubElement(channel, "language").text    = PODCAST_LANG

    itag(channel, "author",   PODCAST_AUTHOR)
    itag(channel, "explicit", PODCAST_EXPLICIT)
    itag(channel, "type",     "episodic")
    cat = itag(channel, "category")
    cat.set("text", PODCAST_CATEGORY)
    img = itag(channel, "image")
    img.set("href", cover_url)

    if PODCAST_EMAIL:
        owner = ET.SubElement(channel, f"itunes:owner")
        ET.SubElement(owner, f"itunes:name").text  = PODCAST_AUTHOR
        ET.SubElement(owner, f"itunes:email").text = PODCAST_EMAIL

    image_el = ET.SubElement(channel, "image")
    ET.SubElement(image_el, "url").text   = cover_url
    ET.SubElement(image_el, "title").text = PODCAST_TITLE
    ET.SubElement(image_el, "link").text  = base_url()

    # --- Episoden hinzufügen (neueste zuerst im Feed, älteste = Episode #1) ---
    for episode_number, mp3_path in enumerate(mp3_files, start=1):
        date_str = mp3_path.stem  # z.B. "2026-04-14"

        # Datum parsen
        try:
            ep_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"   ⚠️  Dateiname '{mp3_path.name}' hat kein gültiges Datum – überspringe.")
            continue

        ep_title = f"{PODCAST_TITLE} – {ep_date.strftime('%d.%m.%Y')}"
        audio_url = f"{base_url()}/episodes/{mp3_path.name}"
        audio_size = mp3_path.stat().st_size

        # pubDate: RFC 2822 Format, 06:00 UTC (≈ Veröffentlichungszeit)
        pub_date = formatdate(timeval=timegm(ep_date.replace(hour=6).timetuple()), localtime=False)

        # Beschreibung und Titel aus .txt-Datei lesen, falls vorhanden
        txt_path = episodes_dir / f"{date_str}.txt"
        if txt_path.exists():
            full_txt = txt_path.read_text(encoding="utf-8").strip()
            if "===" in full_txt:
                parts = full_txt.split("===", 1)
                meta = parts[0].strip()
                script_text = parts[1].strip()
                
                # Titel aus Meta-Informationen extrahieren
                for line in meta.splitlines():
                    if line.startswith("TITEL:"):
                        ep_title = f"{PODCAST_TITLE} – {ep_date.strftime('%d.%m.%Y')}: {line.replace('TITEL:', '').strip()}"
            else:
                script_text = full_txt
            
            # Erste 300 Zeichen des Skripts als Beschreibung (ohne Break-Tags)
            desc_text = re.sub(r'<break[^>]*/?>', '', script_text)[:300].strip()
            if len(script_text) > 300:
                desc_text += " ..."
            episode_desc = desc_text
        else:
            episode_desc = f"Die wichtigsten Nachrichten zu erneuerbaren Energien vom {ep_date.strftime('%d.%m.%Y')}."

        # Item erstellen
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text       = ep_title
        ET.SubElement(item, "description").text = episode_desc
        ET.SubElement(item, "pubDate").text     = pub_date
        guid_el = ET.SubElement(item, "guid")
        guid_el.text = audio_url
        guid_el.set("isPermaLink", "true")
        ET.SubElement(item, "link").text        = audio_url

        enclosure = ET.SubElement(item, "enclosure")
        enclosure.set("url",    audio_url)
        enclosure.set("type",   "audio/mpeg")
        enclosure.set("length", str(audio_size))

        itag(item, "title",       ep_title)
        itag(item, "summary",     episode_desc)
        itag(item, "explicit",    PODCAST_EXPLICIT)
        itag(item, "episodeType", "full")
        itag(item, "episode",     str(episode_number))
        ep_img = itag(item, "image")
        ep_img.set("href", cover_url)

    # Episoden im Channel: neueste zuerst (RSS-Standard)
    items = channel.findall("item")
    non_items = [el for el in channel if el.tag != "item"]
    for item in items:
        channel.remove(item)
    for item in reversed(items):  # reversed: älteste waren zuerst, jetzt neueste zuerst
        channel.append(item)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    tree.write(feed_path, encoding="unicode", xml_declaration=True)
    print(f"   feed.xml gespeichert ({len(items)} Episoden).")


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
    elif not cover_dst.exists():
        print("⚠️  Kein cover.jpg gefunden – RSS-Feed referenziert fehlendes Cover-Bild.")

    audio_filename = f"{date_str}.mp3"
    audio_path = str(episodes_dir / audio_filename)
    script_path = episodes_dir / f"{date_str}.txt"

    # Doppelte Ausführung verhindern (z.B. bei Sommer/Winterzeit-Doppel-Cron)
    if Path(audio_path).exists():
        print(f"⏭️  Episode für heute ({date_str}) existiert bereits – überspringe.")
        return

    memory = load_memory()
    prompt_config = load_prompt_config()
    articles = fetch_all_news(memory)
    hot_topics = get_hot_topics(memory)
    if hot_topics:
        print(f"🔥 Aktuelle Hot-Topics: {', '.join(hot_topics)}")
    script, selected_indices, title_tag = generate_script(articles, prompt_config, hot_topics)

    # Relevante Artikel für Archiv und Faktencheck auswählen
    if selected_indices:
        selected_articles = [articles[i] for i in selected_indices]
    else:
        selected_articles = articles  # Fallback: alle

    # Faktencheck-Schleife
    script = fact_check_script(script, selected_articles, prompt_config)

    # Datei-Inhalt vorbereiten (Metadaten + Skript)
    file_content = ""
    if title_tag:
        file_content += f"TITEL: {title_tag}\n===\n"
    file_content += script
    
    script_path.write_text(file_content, encoding="utf-8")

    # Schritt 1: Nur Sprache generieren (temporäre Datei)
    speech_path = str(episodes_dir / f"{date_str}_speech.mp3")
    generate_audio(script, speech_path)

    # Schritt 2: Jingle vorne und hinten einbauen -> finale Episode
    combine_with_jingle(speech_path, audio_path)

    # Temporäre Sprach-Datei aufräumen
    Path(speech_path).unlink(missing_ok=True)

    # Nur tatsächlich verwendete Artikel ins Archiv eintragen
    memory = add_to_archive(selected_articles, memory, episode_title)
    save_memory(memory)

    # RSS-Feed komplett neu aufbauen (scannt alle MP3s in docs/episodes/)
    rebuild_rss_feed()

    print("\n✅ Fertig!")
    print(f"   Richtlinien : {PROMPT_FILE}")
    print(f"   Datenbank   : {MEMORY_FILE} ({len(memory['archive'])} Einträge gesamt)")
    print(f"   Modell      : claude-sonnet-4-6 + ElevenLabs")
    print(f"   Skript      : {script_path}")
    print(f"   Audio       : {audio_path}")
    print(f"   Feed        : docs/feed.xml")
    print(f"   URL         : {base_url()}/episodes/{audio_filename}")


# ---------------------------------------------------------------------------
# Test-Modus: --test
# ---------------------------------------------------------------------------

def main_test() -> None:
    """
    Testmodus: Führt den kompletten Skript-Generierungsprozess durch
    (News abrufen → Claude-Skript → Faktencheck), speichert das Ergebnis
    als .txt in test-output/ und beendet sich dann.

    Kein ElevenLabs, kein Audio, kein Archiv, kein Memory-Update, kein RSS.
    """
    today = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")

    test_dir = Path("test-output")
    test_dir.mkdir(parents=True, exist_ok=True)
    output_path = test_dir / f"skript_{date_str}.txt"

    print("🧪 TEST-MODUS – kein Audio, kein Archiv, kein Memory-Update")
    print(f"   Ausgabedatei: {output_path}")
    print()

    # Speicher nur lesend laden (für Sperrfrist-Check), aber NICHT speichern
    memory = load_memory()
    prompt_config = load_prompt_config()
    articles = fetch_all_news(memory)
    hot_topics = get_hot_topics(memory)
    if hot_topics:
        print(f"🔥 Aktuelle Hot-Topics: {', '.join(hot_topics)}")

    script, selected_indices, title_tag = generate_script(articles, prompt_config, hot_topics)

    if selected_indices:
        selected_articles = [articles[i] for i in selected_indices]
    else:
        selected_articles = articles  # Fallback

    script = fact_check_script(script, selected_articles, prompt_config)

    # Datei-Inhalt vorbereiten (Metadaten + Skript)
    file_content = ""
    if title_tag:
        file_content += f"TITEL: {title_tag}\n===\n"
    file_content += script

    output_path.write_text(file_content, encoding="utf-8")

    print()
    print("✅ Test abgeschlossen – Skript gespeichert (kein Audio, kein Commit):")
    print(f"   {output_path}")
    print(f"   Wörter: {len(script.split())}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        main_test()
    else:
        main()
