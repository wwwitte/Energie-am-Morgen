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
    """
    Holt die kumulierte installierte Leistung je Energieträger von energy-charts.info.

    Liefert Jahresenddaten (nicht Momentanwerte) – also die gesamte installierte
    Kapazität am Ende des letzten abgeschlossenen Jahres.
    Strategie: Das letzte Jahr mit vollständigen Daten für alle Hauptkategorien verwenden,
    nicht das laufende Jahr (das wäre unvollständig).
    """
    result = {}
    try:
        url = f"{ENERGY_CHARTS_BASE}/installed_power?country=de"
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"   energy-charts: HTTP {r.status_code}")
            return result
        data = r.json()

        production_types = data.get("production_types", [])
        time_series = data.get("time", [])
        if not time_series or not production_types:
            return result

        # Mapping: API-Namen → interne Schlüssel
        # energy-charts liefert kumulierte Jahresend-Kapazität in MW
        category_map = {
            "wind_onshore":  ["wind onshore", "onshore wind"],
            "wind_offshore": ["wind offshore", "offshore wind"],
            "solar":         ["solar", "photovoltaic", "pv"],
            "biomass":       ["biomass", "bioenergy", "biomasse"],
            "hydro":         ["hydro", "run-of-river", "wasserkraft", "laufwasser"],
            "pumped_storage":["pumped storage", "pumpspeicher"],
            "nuclear":       ["nuclear", "kernenergie", "atom"],
            "gas":           ["gas", "natural gas"],
            "coal":          ["hard coal", "steinkohle", "lignite", "braunkohle", "coal"],
        }

        # Letztes vollständig abgeschlossenes Jahr finden:
        # Gehe von hinten durch die Jahresliste und nimm das erste Jahr,
        # bei dem alle drei Hauptkategorien (Wind Onshore, Wind Offshore, Solar)
        # einen non-null Wert haben.
        ref_idx = None
        for idx in range(len(time_series) - 1, -1, -1):
            year_val = time_series[idx]
            # Laufendes Jahr überspringen
            current_year = datetime.date.today().year
            if isinstance(year_val, int) and year_val >= current_year:
                continue
            # Prüfen ob Hauptkategorien Werte haben
            main_categories_ok = 0
            for pt in production_types:
                name = pt.get("name", "").lower()
                values = pt.get("data", [])
                if idx < len(values) and values[idx] is not None:
                    if any(kw in name for kw in ["wind", "solar", "photovoltaic"]):
                        main_categories_ok += 1
            if main_categories_ok >= 2:
                ref_idx = idx
                break

        if ref_idx is None:
            # Fallback: vorletzter Index (zweitletztes Jahr)
            ref_idx = max(0, len(time_series) - 2)

        ref_year = time_series[ref_idx]
        result["year"] = ref_year

        # Werte für den Referenz-Index extrahieren
        for pt in production_types:
            name = pt.get("name", "").lower()
            values = pt.get("data", [])
            if ref_idx >= len(values) or values[ref_idx] is None:
                continue
            val_mw = values[ref_idx]
            val_gw = round(val_mw / 1000, 1)  # MW → GW

            for key, keywords in category_map.items():
                if any(kw in name for kw in keywords):
                    # Bei Kohle: addieren (Stein- + Braunkohle)
                    if key == "coal" and key in result:
                        result[key] = round(result[key] + val_gw, 1)
                    else:
                        result[key] = val_gw
                    break

        # Gesamte Windkraft berechnen
        wind_total = round(
            result.get("wind_onshore", 0) + result.get("wind_offshore", 0), 1
        )
        if wind_total > 0:
            result["wind_total"] = wind_total

        # Gesamt Erneuerbare berechnen
        renewable_total = round(sum(
            result.get(k, 0)
            for k in ["wind_onshore", "wind_offshore", "solar", "biomass", "hydro", "pumped_storage"]
        ), 1)
        if renewable_total > 0:
            result["renewable_total"] = renewable_total

        print(f"   energy-charts installierte Leistung (Stand {ref_year}):")
        for key in ["wind_onshore", "wind_offshore", "wind_total", "solar",
                    "biomass", "hydro", "renewable_total"]:
            if key in result:
                print(f"     {key}: {result[key]} GW")

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
    dyn_lines = ["[INSTALLIERTE LEISTUNG – KUMULIERT (JAHRESENDWERTE, AUTOMATISCH AKTUALISIERT)]"]

    if installed:
        year = installed.get("year", "aktuell")
        source = f"energy-charts.info / Fraunhofer ISE, Stand Ende {year}"

        # Windkraft
        if "wind_onshore" in installed:
            dyn_lines.append(f"Windkraft Onshore installiert gesamt: {installed['wind_onshore']} Gigawatt ({source})")
        if "wind_offshore" in installed:
            dyn_lines.append(f"Windkraft Offshore installiert gesamt: {installed['wind_offshore']} Gigawatt ({source})")
        if "wind_total" in installed:
            dyn_lines.append(f"Windkraft gesamt installiert: {installed['wind_total']} Gigawatt ({source})")

        # Solar
        if "solar" in installed:
            dyn_lines.append(f"Photovoltaik installiert gesamt: {installed['solar']} Gigawatt ({source})")

        # Weitere Erneuerbare
        if "biomass" in installed:
            dyn_lines.append(f"Biomasse installiert gesamt: {installed['biomass']} Gigawatt ({source})")
        if "hydro" in installed:
            dyn_lines.append(f"Wasserkraft installiert gesamt: {installed['hydro']} Gigawatt ({source})")

        # Gesamte Erneuerbare
        if "renewable_total" in installed:
            dyn_lines.append(f"Erneuerbare Energien installiert gesamt: {installed['renewable_total']} Gigawatt ({source})")

    else:
        # Fallback auf manuell geprüfte Werte (Stand Ende 2025)
        dyn_lines.append("Photovoltaik installiert gesamt: 118 Gigawatt (Fraunhofer ISE, Stand Ende 2025, Fallback)")
        dyn_lines.append("Windkraft Onshore installiert gesamt: 62 Gigawatt (BDEW, Stand Ende 2025, Fallback)")
        dyn_lines.append("Windkraft Offshore installiert gesamt: 9 Gigawatt (BDEW, Stand Ende 2025, Fallback)")
        dyn_lines.append("Windkraft gesamt installiert: 71 Gigawatt (BDEW, Stand Ende 2025, Fallback)")

    dyn_lines.append("")
    dyn_lines.append("[STROMMIX – AUTOMATISCH AKTUALISIERT]")

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
