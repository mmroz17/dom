import os, json, time, subprocess, datetime as dt, urllib.request
from zoneinfo import ZoneInfo
import tinytuya

LAT, LON = 50.073, 20.034          # Kraków, Osiedle Oświecenia
TZ = ZoneInfo("Europe/Warsaw")
NTFY = os.environ["NTFY_TOPIC"].strip()
INDOOR = [x.strip() for x in os.environ["TUYA_INDOOR_IDS"].split(",") if x.strip()]
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


def target(hour):
    if 6 <= hour < 9: return 21.0    # rano cieplej
    if 9 <= hour < 18: return 20.5   # dzień (oszczędnie)
    if 18 <= hour < 22: return 20.0  # wieczór chłodniej
    return 18.0                      # noc / sen


def advise(now, ti, hi, to, ho, pm25, pm10):
    tgt, h, out = target(now.hour), now.hour, []
    bad_air = pm25 > 35 or pm10 > 50
    cold_out = to < 5
    mins = 5 if to < 0 else 10

    if bad_air:
        out.append(("smog", "Smog: okna zamknięte",
                    f"PM2.5: {pm25:.0f}, PM10: {pm10:.0f} µg/m³. Nie wietrz, włącz oczyszczacz (tryb auto/wysoki).", 4))

    if ti > tgt + 1:
        if to < ti - 2 and not bad_air:
            out.append(("otworz", "Otwórz okna",
                        f"W domu {ti:.1f}°C (cel {tgt:.0f}°C), na balkonie {to:.1f}°C. Wywietrz {mins} min.", 3))
        else:
            out.append(("przykrec", "Przykręć kaloryfery",
                        f"W domu {ti:.1f}°C, cel {tgt:.0f}°C. Przykręć zawory o 1 stopień.", 3))
    elif ti < tgt - 1:
        if to > ti + 1 and not bad_air:
            out.append(("otworz", "Otwórz okna",
                        f"W domu {ti:.1f}°C, a na balkonie ciepleje: {to:.1f}°C. Wywietrz {mins} min.", 3))
        else:
            out.append(("odkrec", "Odkręć kaloryfery",
                        f"W domu {ti:.1f}°C, cel {tgt:.0f}°C. Odkręć zawory o 1 stopień i sprawdź, czy okna są zamknięte.", 3))

    if hi is not None:
        if hi > 60:
            if ho is not None and ho < hi - 10 and not bad_air:
                out.append(("wilg_wys", "Wilgotność za wysoka",
                            f"W domu {hi:.0f}%. Wywietrz {mins} min (łazienka, kuchnia).", 3))
            else:
                out.append(("wilg_wys", "Wilgotność za wysoka",
                            f"W domu {hi:.0f}%. Włącz wentylację w łazience, nie susz prania w domu.", 3))
        elif hi < 35:
            out.append(("wilg_nis", "Powietrze za suche",
                        f"W domu {hi:.0f}%. Nie wietrz długo, rozwieś pranie lub postaw miskę z wodą.", 2))

    if h == 21 and ti > 19.5:
        out.append(("sen", "Przygotuj sypialnię",
                    f"Do spania lepiej ~18°C. Przykręć grzejnik w sypialni (teraz w domu {ti:.1f}°C).", 2))

    if not out and 7 <= h < 9 and not bad_air and to > -15:
        out.append(("poranek", "Poranne wietrzenie",
                    f"Powietrze OK (PM2.5: {pm25:.0f}). Wywietrz mieszkanie {mins} min, a potem zamknij okna.", 2))
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


def save_dashboard(now, ti, hi, to, ho, air, items, new_alert):
    r = lambda v: None if v is None else round(v)
    hist = load("history.json", [])
    # usuń błędne odczyty z wcześniejszych uruchomień
    hist = [p for p in hist if abs(p["ti"]) <= 60 and abs(p["to"]) <= 60
            and (p["hi"] is None or p["hi"] <= 100) and (p["ho"] is None or p["ho"] <= 100)]
    hist.append({"t": now.isoformat(timespec="minutes"), "ti": round(ti, 1), "hi": r(hi),
                 "to": round(to, 1), "ho": r(ho), "pm25": air["pm2_5"], "pm10": air["pm10"]})
    hist = hist[-1344:]  # ok. 28 dni
    json.dump(hist, open("history.json", "w"), separators=(",", ":"))
    alerts = load("data.json", {}).get("alerts", [])
    if new_alert:
        alerts = (alerts + [new_alert])[-40:]
    json.dump({"updated": now.isoformat(timespec="minutes"), "lat": LAT, "lon": LON,
               "target": target(now.hour), "now": hist[-1], "alerts": alerts,
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
                           apiSecret=os.environ["TUYA_SECRET"].strip(), apiDeviceID=INDOOR[0])
    ins = [r for r in (read_sensor(cloud, d) for d in INDOOR) if r]
    outs = read_sensor(cloud, OUTDOOR)

    if not ins or not outs:
        if state.get("key") != "offline":
            notify("Czujniki nie odpowiadają",
                   "Sprawdź baterie lub połączenie. Możliwe, że wygasł dostęp próbny w Tuya IoT Platform (do przedłużenia za darmo).", 4)
        state.update(key="offline")
        json.dump(state, open(STATE_FILE, "w"))
        return

    ti = sum(r[0] for r in ins) / len(ins)
    hs = [r[1] for r in ins if r[1] is not None]
    hi = sum(hs) / len(hs) if hs else None
    to, ho = outs

    air = get("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s"
              "&current=pm2_5,pm10" % (LAT, LON))["current"]
    items = advise(now, ti, hi, to, ho, air["pm2_5"], air["pm10"])
    key = "|".join(k for k, *_ in items)
    last = dt.datetime.fromisoformat(state["sent"]) if state.get("sent") else None
    stale = last is None or (now - last).total_seconds() > 4 * 3600

    new_alert = None
    if items and (key != state.get("key") or stale):
        title = items[0][1]
        msg = "\n\n".join(m for _, _, m, _ in items)
        notify(title, msg, max(p for *_, p in items))
        state["sent"] = now.isoformat()
        new_alert = {"t": now.isoformat(timespec="minutes"), "title": title, "msg": msg}
    save_dashboard(now, ti, hi, to, ho, air, items, new_alert)
    state["key"] = key
    json.dump(state, open(STATE_FILE, "w"))
    print(f"dom {ti:.1f}°C/{hi}%  balkon {to:.1f}°C  PM2.5 {air['pm2_5']}  porady: {key or '-'}")


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
