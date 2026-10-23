import os, json, time, subprocess, datetime as dt, urllib.request
from zoneinfo import ZoneInfo
import tinytuya

LAT, LON = 50.073, 20.034          # Kraków, Osiedle Oświecenia
TZ = ZoneInfo("Europe/Warsaw")
NTFY = os.environ["NTFY_TOPIC"].strip()
def parse_rooms(raw):
    items = [x.strip() for x in raw.split(",") if x.strip()]
    rooms = []
    for i, x in enumerate(items):
        name, sep, dev = x.partition("=")
        if not sep:
            name, dev = ("Mieszkanie" if len(items) == 1 else f"Pokój {i + 1}"), x
        rooms.append((name.strip(), dev.strip()))
    return rooms


ROOMS = parse_rooms(os.environ["TUYA_INDOOR_IDS"])
OUTDOOR = os.environ["TUYA_OUTDOOR_ID"].strip()
STATE_FILE = "state.json"

WMO = {0: "słonecznie", 1: "prawie bezchmurnie", 2: "częściowe zachmurzenie", 3: "pochmurno",
       45: "mgła", 48: "mgła", 51: "mżawka", 53: "mżawka", 55: "mżawka", 61: "deszcz",
       63: "deszcz", 65: "silny deszcz", 71: "śnieg", 73: "śnieg", 75: "silny śnieg",
       80: "przelotny deszcz", 81: "przelotny deszcz", 82: "ulewy", 95: "burza"}


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def notify(title, msg, prio=3, tags=None):
    body = json.dumps({"topic": NTFY, "title": title, "message": msg,
                       "priority": prio, "tags": tags or []}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh", data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def read_sensor(cloud, dev):
    res = cloud.getstatus(dev)
    print(dev, "->", res)  # podgląd surowych danych (pomaga w razie problemów)
    st = {i["code"]: i["value"] for i in res.get("result", [])}
    t = next((st[k] for k in ("va_temperature", "temp_current", "temperature") if k in st), None)
    h = next((st[k] for k in ("va_humidity", "humidity_value", "humidity") if k in st), None)
    if t is None:
        return None
    while abs(t) > 60:   # Tuya podaje liczby bez przecinka (np. 2203 = 22,03°C)
        t /= 10
    if h is not None:
        while h > 100:
            h /= 10
    return t, h


def target(name, hour):
    n = name.lower()
    if "łazien" in n or "lazien" in n:
        return 23.5 if 6 <= hour < 9 else 23.0 if 18 <= hour < 22 else 21.5
    if "sypial" in n:
        return 21.5 if 6 <= hour < 9 else 20.5 if 9 <= hour < 21 else 19.0
    if 6 <= hour < 9: return 22.0
    if 9 <= hour < 18: return 21.5
    if 18 <= hour < 22: return 21.0
    return 19.5


def vent_min(to):
    return 5 if to < 0 else 7 if to < 10 else 10


def air_label(p):
    return "bardzo dobre" if p <= 15 else "dobre" if p <= 35 else "umiarkowane" if p <= 55 else "złe"


def conditions(rooms, to, ho, pm25, pm10):
    lines = ["Warunki:"]
    for r in rooms:
        hum = f", {r['h']:.0f}%" if r["h"] is not None else ""
        lines.append(f"• {r['name']}: {r['t']:.1f}°C{hum} (cel {r['target']:.1f}°C)")
    hum = f", {ho:.0f}%" if ho is not None else ""
    lines.append(f"• Balkon: {to:.1f}°C{hum}")
    lines.append(f"• Powietrze: PM2.5 {pm25:.0f}, PM10 {pm10:.0f} ({air_label(pm25)})")
    return "\n".join(lines)


def advise(now, rooms, to, ho, pm25, pm10):
    h, out = now.hour, []
    bad_air = pm25 > 35 or pm10 > 50
    m = vent_min(to)
    for r in rooms:
        r["target"] = target(r["name"], h)

    if bad_air:
        out.append(("smog", "Smog: nie wietrz",
                    f"PM2.5 {pm25:.0f}, PM10 {pm10:.0f} µg/m³. Zamknij okna we wszystkich pokojach "
                    "i włącz oczyszczacz (tryb auto lub wysoki).", 4))

    hot = [r for r in rooms if r["t"] > r["target"] + 1.5]
    cold = [r for r in rooms if r["t"] < r["target"] - 0.7]

    if hot:
        coolest = min(r["t"] for r in hot)
        if to < coolest - 3 and not bad_air:
            lines = [f"• {r['name']}: {r['t']:.1f}°C (cel {r['target']:.1f}). Otwórz okno szeroko na {m} min, potem zamknij."
                     for r in hot]
            out.append(("otworz:" + ",".join(r["name"] for r in hot), "Za ciepło: otwórz okna", "\n".join(lines), 3))
        else:
            lines = [f"• {r['name']}: {r['t']:.1f}°C (cel {r['target']:.1f}). Przykręć zawór kaloryfera o 1 stopień (np. z 3 na 2)."
                     for r in hot]
            out.append(("przykrec:" + ",".join(r["name"] for r in hot), "Za ciepło: przykręć kaloryfery", "\n".join(lines), 3))

    if cold:
        if to > max(r["t"] for r in cold) + 1 and not bad_air:
            lines = [f"• {r['name']}: {r['t']:.1f}°C (cel {r['target']:.1f}). Na balkonie jest cieplej ({to:.1f}°C): wywietrz {m} min."
                     for r in cold]
            out.append(("ciepl:" + ",".join(r["name"] for r in cold), "Chłodno: wywietrz ciepłym powietrzem", "\n".join(lines), 3))
        else:
            lines = [f"• {r['name']}: {r['t']:.1f}°C (cel {r['target']:.1f}). Odkręć zawór kaloryfera o 1 stopień (np. z 2 na 3) i sprawdź, czy okno jest zamknięte."
                     for r in cold]
            out.append(("odkrec:" + ",".join(r["name"] for r in cold), "Chłodno: odkręć kaloryfery", "\n".join(lines), 3))

    wet, dry = [], []
    for r in rooms:
        if r["h"] is None:
            continue
        lim = 75 if ("łazien" in r["name"].lower() or "lazien" in r["name"].lower()) else 60
        if r["h"] > lim: wet.append(r)
        elif r["h"] < 35: dry.append(r)
    if wet:
        can_vent = ho is not None and not bad_air and all(ho < r["h"] - 10 for r in wet)
        lines = [f"• {r['name']}: wilgotność {r['h']:.0f}%. " +
                 (f"Wywietrz {m} min." if can_vent else "Włącz wentylację/oczyszczacz, zamknij drzwi do reszty mieszkania, nie susz tu prania.")
                 for r in wet]
        out.append(("wilg:" + ",".join(r["name"] for r in wet), "Za wilgotno", "\n".join(lines), 3))
    if dry:
        lines = [f"• {r['name']}: wilgotność {r['h']:.0f}%. Nie wietrz długo, rozwieś pranie lub postaw miskę z wodą." for r in dry]
        out.append(("sucho:" + ",".join(r["name"] for r in dry), "Za sucho", "\n".join(lines), 2))

    for r in rooms:
        if "sypial" in r["name"].lower() and 21 <= h < 23 and r["t"] > 20.5:
            out.append(("sen", "Sypialnia przed snem",
                        f"• {r['name']}: {r['t']:.1f}°C, do spania ok. 19°C. Przykręć zawór o 1 stopień"
                        + ("" if bad_air else f" i wywietrz {m} min przed snem.") , 2))

    if not out and 7 <= h < 9 and not bad_air and to > -15:
        out.append(("poranek", "Poranne wietrzenie",
                    f"Powietrze dobre. Otwórz okna szeroko w całym mieszkaniu na {m} min (krótko, żeby nie wychłodzić ścian), potem zamknij.", 2))
    return out


def briefing():
    w = get("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
            "&current=temperature_2m,weather_code"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
            "&timezone=Europe%%2FWarsaw&forecast_days=4" % (LAT, LON))
    d = w["daily"]
    days = ["dziś", "jutro", "pojutrze", "za 3 dni"]
    lines = []
    for i in range(4):
        desc = WMO.get(d["weather_code"][i], "")
        rain = f", opad {d['precipitation_sum'][i]:.1f} mm" if d["precipitation_sum"][i] >= 0.5 else ""
        lines.append(f"{days[i]}: {d['temperature_2m_min'][i]:.0f}…{d['temperature_2m_max'][i]:.0f}°C, {desc}{rain}")
    cur = w["current"]
    notify("Pogoda na dziś", f"Teraz {cur['temperature_2m']:.0f}°C, {WMO.get(cur['weather_code'], '')}\n" + "\n".join(lines),
           2, ["sunny"])


def load(f, default):
    try:
        return json.load(open(f))
    except Exception:
        return default


def save_dashboard(now, rooms, to, ho, air, items, new_alert):
    r0 = lambda v: None if v is None else round(v)
    hist = load("history.json", [])
    hist = [p for p in hist if abs(p["ti"]) <= 60 and abs(p["to"]) <= 60
            and (p["hi"] is None or p["hi"] <= 100) and (p["ho"] is None or p["ho"] <= 100)]
    hs = [r["h"] for r in rooms if r["h"] is not None]
    ti = sum(r["t"] for r in rooms) / len(rooms)
    hist.append({"t": now.isoformat(timespec="minutes"), "ti": round(ti, 1),
                 "hi": r0(sum(hs) / len(hs)) if hs else None,
                 "to": round(to, 1), "ho": r0(ho), "pm25": air["pm2_5"], "pm10": air["pm10"],
                 "rm": {r["name"]: [round(r["t"], 1), r0(r["h"])] for r in rooms}})
    hist = hist[-1344:]  # ok. 28 dni
    json.dump(hist, open("history.json", "w"), separators=(",", ":"), ensure_ascii=False)
    alerts = load("data.json", {}).get("alerts", [])
    if new_alert:
        alerts = (alerts + [new_alert])[-40:]
    json.dump({"updated": now.isoformat(timespec="minutes"), "lat": LAT, "lon": LON,
               "target": round(sum(r["target"] for r in rooms) / len(rooms), 1),
               "now": hist[-1], "alerts": alerts,
               "rooms": [{"name": r["name"], "t": round(r["t"], 1), "h": r0(r["h"]), "target": r["target"]} for r in rooms],
               "advice": [{"title": t, "msg": m} for _, t, m, _ in items]},
              open("data.json", "w"), ensure_ascii=False)


def main():
    now = dt.datetime.now(TZ)
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}

    # poranny raport pogody (raz dziennie, 7:00-10:59)
    if 7 <= now.hour < 11 and state.get("briefing") != str(now.date()):
        try:
            briefing()
            state["briefing"] = str(now.date())
        except Exception as e:
            print("briefing error", e)

    cloud = tinytuya.Cloud(apiRegion="eu", apiKey=os.environ["TUYA_KEY"].strip(),
                           apiSecret=os.environ["TUYA_SECRET"].strip(), apiDeviceID=ROOMS[0][1])
    rooms = []
    for name, dev in ROOMS:
        rd = read_sensor(cloud, dev)
        if rd:
            rooms.append({"name": name, "t": rd[0], "h": rd[1]})
    outs = read_sensor(cloud, OUTDOOR)

    if not rooms or not outs:
        if state.get("key") != "offline":
            notify("Czujniki nie odpowiadają",
                   "Sprawdź baterie lub połączenie. Możliwe, że wygasł dostęp próbny w Tuya IoT Platform (do przedłużenia za darmo).", 4)
        state.update(key="offline")
        json.dump(state, open(STATE_FILE, "w"))
        return

    to, ho = outs

    air = get("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s"
              "&current=pm2_5,pm10" % (LAT, LON))["current"]
    items = advise(now, rooms, to, ho, air["pm2_5"], air["pm10"])
    key = "|".join(k for k, *_ in items)
    last = dt.datetime.fromisoformat(state["sent"]) if state.get("sent") else None
    stale = last is None or (now - last).total_seconds() > 4 * 3600

    new_alert = None
    if items and (key != state.get("key") or stale):
        title = items[0][1]
        msg = "\n\n".join(m for _, _, m, _ in items) + "\n\n" + conditions(rooms, to, ho, air["pm2_5"], air["pm10"])
        notify(title, msg, max(p for *_, p in items))
        state["sent"] = now.isoformat()
        new_alert = {"t": now.isoformat(timespec="minutes"), "title": title, "msg": msg}
    save_dashboard(now, rooms, to, ho, air, items, new_alert)
    state["key"] = key
    json.dump(state, open(STATE_FILE, "w"))
    print("pokoje:", [(r["name"], r["t"], r["h"]) for r in rooms], "balkon:", to, ho, "PM2.5:", air["pm2_5"], "porady:", key or "-")


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True)


def publish():
    """Zapisuje wyniki w repozytorium (z ponawianiem, gdy ktoś równocześnie coś zmienił)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    sh("git", "config", "user.name", "bot")
    sh("git", "config", "user.email", "bot@users.noreply.github.com")
    sh("git", "add", "state.json", "data.json", "history.json")
    if sh("git", "diff", "--cached", "--quiet").returncode == 0:
        return
    sh("git", "commit", "-m", "state")
    for _ in range(4):
        r = sh("git", "pull", "--rebase", "-X", "theirs")
        if r.returncode != 0:
            sh("git", "rebase", "--abort")
        r = sh("git", "push")
        print(r.stdout, r.stderr)
        if r.returncode == 0:
            return
        time.sleep(3)
    raise SystemExit("Nie udało się zapisać wyników w repozytorium")


if __name__ == "__main__":
    try:
        main()
    finally:
        publish()
