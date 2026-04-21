"""
update_facts.py – Automatische Aktualisierung der Faktenbasis
=============================================================
Ruft aktuelle Energiedaten von offiziellen APIs ab und
aktualisiert facts.txt mit verifizierten Zahlen.

Quellen:
  - SMARD (Bundesnetzagentur) – Strommix, Preise
  - energy-charts.info (Fraunhofer ISE) – installierte Leistung
  - Statische Fallback-Werte falls APIs nicht erreichbar

Aufruf:
  python update_facts.py
  oder automatisch via GitHub Actions (vor podcast_generator.py)
"""

import datetime
import json
import time
from pathlib import Path

import requests

FACTS_FILE = "facts.txt"
TIMEOUT    = 15  # Sekunden pro API-Request

# ---------------------------------------------------------------------------
# SMARD API (Bundesnetzagentur) – kostenlos, keine Auth nötig
# Doku: https://www.smard.de/home/downloadcenter/download-marktdaten
# ---------------------------------------------------------------------------

SMARD_BASE = "https://www.smard.de/app/chart_data"

# Filter-IDs für SMARD
SMARD_FILTERS = {
    "wind_onshore":  189,
    "wind_offshore": 190,
    "solar":         191,
    "total_load":    410,
    "price_de":      4169,
}

def smard_get_latest(filter_id: int, region: str = "DE") -> float | None:
    """Holt den letzten verfügbaren Wert aus der SMARD-API."""
    try:
        # Erst Index abrufen um verfügbare Zeitstempel zu finden
        index_url = f"{SMARD_BASE}/{filter_id}/{region}/{filter_id}_{region}_quarterhour_index.json"
        r = requests.get(index_url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        timestamps = r.json().get("timestamps", [])
        if not timestamps:
            return None

        # Letzten Zeitstempel-Block abrufen
        latest_ts = timestamps[-1]
        data_url = f"{SMARD_BASE}/{filter_id}/{region}/{filter_id}_{region}_quarterhour_{latest_ts}.json"
        r2 = requests.get(data_url, timeout=TIMEOUT)
        if r2.status_code != 200:
            return None

        series = r2.json().get("series", [])
        # Letzten nicht-null Wert finden
        for ts, val in reversed(series):
            if val is not None:
                return round(val, 1)
        return None
    except Exception as e:
        print(f"   SMARD Fehler (filter {filter_id}): {e}")
        return None


# ---------------------------------------------------------------------------
# energy-charts.info API (Fraunhofer ISE)
# ---------------------------------------------------------------------------

ENERGY_CHARTS_BASE = "https://api.energy-charts.info"

def get_installed_power() -> dict:
    """Holt installierte Leistung Wind/Solar von energy-charts.info."""
    result = {}
    try:
        url = f"{ENERGY_CHARTS_BASE}/installed_power?country=de"
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return result
        data = r.json()

        # Neueste Jahreswerte extrahieren
        production_types = data.get("production_types", [])
        time_series = data.get("time", [])
        if not time_series:
            return result

        latest_idx = len(time_series) - 1

        for pt in production_types:
            name = pt.get("name", "").lower()
            values = pt.get("data", [])
            if latest_idx < len(values) and values[latest_idx] is not None:
                val_gw = round(values[latest_idx] / 1000, 1)  # MW → GW
                if "wind" in name and "offshore" in name:
                    result["wind_offshore_gw"] = val_gw
                elif "wind" in name and "onshore" in name:
                    result["wind_onshore_gw"] = val_gw
                elif "solar" in name or "photovoltaic" in name:
                    result["solar_gw"] = val_gw

        latest_year = time_series[latest_idx]
        result["year"] = latest_year
        print(f"   energy-charts: Wind Onshore {result.get('wind_onshore_gw')} GW, "
              f"Wind Offshore {result.get('wind_offshore_gw')} GW, "
              f"Solar {result.get('solar_gw')} GW (Stand {latest_year})")
    except Exception as e:
        print(f"   energy-charts Fehler: {e}")
    return result


def get_power_generation_share() -> dict:
    """Holt den aktuellen Erneuerbare-Anteil am Strommix."""
    result = {}
    try:
        today = datetime.date.today()
        # Letzten vollen Monat abrufen
        first_of_month = today.replace(day=1)
        last_month_end = first_of_month - datetime.timedelta(days=1)
        start = last_month_end.replace(day=1).strftime("%Y-%m-%dT00:00Z")
        end   = last_month_end.strftime("%Y-%m-%dT23:59Z")

        url = (f"{ENERGY_CHARTS_BASE}/public_power"
               f"?country=de&start={start}&end={end}")
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return result

        data = r.json()
        production = data.get("production_types", [])
        total = 0
        renewable = 0

        renewable_types = {"wind", "solar", "hydro", "biomass", "geothermal",
                           "pumped_storage", "run_of_river"}

        for pt in production:
            name = pt.get("name", "").lower().replace(" ", "_")
            values = pt.get("data", [])
            avg = sum(v for v in values if v is not None) / max(len([v for v in values if v is not None]), 1)
            total += avg
            if any(rt in name for rt in renewable_types):
                renewable += avg

        if total > 0:
            share = round(renewable / total * 100, 1)
            result["renewable_share_pct"] = share
            result["period"] = last_month_end.strftime("%m/%Y")
            print(f"   Erneuerbare-Anteil: {share}% ({result['period']})")
    except Exception as e:
        print(f"   Strommix-Anteil Fehler: {e}")
    return result


# ---------------------------------------------------------------------------
# Statische Basis-Fakten (aus offiziellen Quellen manuell gepflegt)
# Diese werden IMMER geschrieben und durch API-Daten ergänzt
# ---------------------------------------------------------------------------

STATIC_FACTS = """[KLIMAZIELE DEUTSCHLAND]
Klimaneutralität Deutschland: 2045 (Klimaschutzgesetz)
THG-Reduktion 2030 vs. 1990: 65 Prozent (Klimaschutzgesetz)
THG-Reduktion 2040 vs. 1990: 88 Prozent (Klimaschutzgesetz)
Erneuerbaren-Ziel 2030 Strommix: 80 Prozent (EEG 2023)

[PHOTOVOLTAIK - GESETZLICHE ZIELE]
PV Ausbauziel 2030: 215 Gigawatt (EEG 2023)
PV notwendiger Zubau jährlich bis 2030: 22 Gigawatt (Fraunhofer ISE)
Balkonsolaranlagen: ca. 4,2 Millionen (UBA, Stand 2025)

[WINDKRAFT - GESETZLICHE ZIELE]
Windkraft Ausbauziel Onshore 2030: 115 Gigawatt (EEG 2023)
Windkraft Ausbauziel Offshore 2030: 30 Gigawatt (EEG 2023)
Windkraft Zubau Onshore 2025: 4,5 Gigawatt (BDEW)
Genehmigte Windanlagen 2025: über 3.300 (Tagesschau)

[NETZ & INFRASTRUKTUR]
Smart Meter Pflichtquote: 20 Prozent (Messstellenbetriebsgesetz)
Bundesnetzagentur Verfahren Smart Meter: 77 (Stand 27.03.2026)
Netzpaket gefährdete Projekte: 32,2 Gigawatt (Enervis-Studie, 03/2026)
Netzpaket gefährdete Investitionen: 45 Milliarden Euro (Enervis-Studie, 03/2026)
Betroffene Landkreise Netzpaket: 90 (Enervis-Studie, 03/2026)

[INVESTITIONEN & CO2]
Investitionen Erneuerbare 2025: 37,6 Milliarden Euro (UBA)
CO2-Einsparung durch Erneuerbare 2025: 265 Millionen Tonnen (UBA)

[AKTUELLE POLITISCHE ROLLEN - DEUTSCHLAND]
Bundeswirtschaftsministerin: Katherina Reiche (CDU, seit 2025)
Bundeskanzler: Friedrich Merz (CDU, seit 2025)
BDEW-Hauptgeschäftsführerin: Kerstin Andreae
Bundesnetzagentur-Präsident: Klaus Müller (Stand 2025)

[REGELN FÜR PERSONEN UND ROLLEN]
Personen NUR erwähnen wenn sie im Quellartikel namentlich genannt sind
Keine Rollen oder Titel erfinden oder ergänzen
Bei Unsicherheit über eine Rolle: Person ohne Titel oder weglassen
Neue Positionen nur berichten wenn der Artikel sie explizit bestätigt

[FORMATREGELN]
Zahlen immer ausschreiben: "dreiundzwanzig Komma eins Prozent" nicht "23,1%"
Einheiten ausschreiben: "Gigawatt" nicht "GW", "Terawattstunden" nicht "TWh"
Datum immer: TT.MM.JJJJ Format
"""


# ---------------------------------------------------------------------------
# Hauptfunktion
# ---------------------------------------------------------------------------

def update_facts() -> None:
    today = datetime.date.today().strftime("%d.%m.%Y")
    print(f"📊 Faktenbasis aktualisieren ({today}) ...")

    lines = [
        f"# Energie am Morgen – Gesicherte Faktenbasis",
        f"# Automatisch aktualisiert: {today}",
        f"# Quellen: SMARD (Bundesnetzagentur), energy-charts.info (Fraunhofer ISE)",
        f"# Manuelle Ergänzungen: statische Ziele und politische Rollen",
        "",
    ]

    # --- API-Daten abrufen ---
    print("🌐 energy-charts.info (Fraunhofer ISE) ...")
    installed = get_installed_power()
    time.sleep(1)

    print("🌐 Strommix-Anteil ...")
    share_data = get_power_generation_share()
    time.sleep(1)

    # --- Dynamischen Block zusammenbauen ---
    dyn_lines = ["[AKTUELLE MESSWERTE – AUTOMATISCH AKTUALISIERT]"]

    if installed:
        year = installed.get("year", "aktuell")
        if "solar_gw" in installed:
            dyn_lines.append(f"PV installierte Leistung: {installed['solar_gw']} Gigawatt (energy-charts.info, Stand {year})")
        if "wind_onshore_gw" in installed:
            dyn_lines.append(f"Windkraft Onshore installiert: {installed['wind_onshore_gw']} Gigawatt (energy-charts.info, Stand {year})")
        if "wind_offshore_gw" in installed:
            dyn_lines.append(f"Windkraft Offshore installiert: {installed['wind_offshore_gw']} Gigawatt (energy-charts.info, Stand {year})")
        wind_total = round(
            installed.get("wind_onshore_gw", 0) + installed.get("wind_offshore_gw", 0), 1
        )
        if wind_total > 0:
            dyn_lines.append(f"Windkraft gesamt installiert: {wind_total} Gigawatt (energy-charts.info, Stand {year})")
    else:
        # Fallback auf manuell geprüfte Werte
        dyn_lines.append("PV installierte Leistung: 118 Gigawatt (Fraunhofer ISE, Stand 01/2026, Fallback)")
        dyn_lines.append("Windkraft gesamt installiert: 68,1 Gigawatt (BDEW, Stand Ende 2025, Fallback)")

    if share_data:
        dyn_lines.append(
            f"Erneuerbare-Anteil Strommix: {share_data['renewable_share_pct']} Prozent "
            f"(energy-charts.info, Stand {share_data['period']})"
        )
    else:
        dyn_lines.append("Erneuerbare-Anteil Strommix 2025: 59 Prozent (Fraunhofer ISE, Fallback)")

    dyn_lines.append("")

    # --- Alles zusammenfügen ---
    lines += dyn_lines
    lines += STATIC_FACTS.strip().split("\n")

    # Datei schreiben
    Path(FACTS_FILE).write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ {FACTS_FILE} aktualisiert ({len(lines)} Zeilen).")


if __name__ == "__main__":
    update_facts()
