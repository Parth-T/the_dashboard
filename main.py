import os
import time
import requests
import serial
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

PORT = os.getenv("SERIAL_PORT", "").strip()
BAUD = 115200

HOME_LAT = float(os.getenv("HOME_LAT", "0"))
HOME_LON = float(os.getenv("HOME_LON", "0"))
DEST_LAT = float(os.getenv("DEST_LAT", "0"))
DEST_LON = float(os.getenv("DEST_LON", "0"))

ORS_API_KEY = os.getenv("ORS_API_KEY", "").strip()
EVENT_TARGET = os.getenv("EVENT_TARGET", "").strip()

PRINT_EVERY_SEC = 1.0
WEATHER_POLL_SEC = 300
TRAFFIC_POLL_SEC = 120

def clamp100(x: float) -> int:
    return max(0, min(100, int(round(x))))

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

def piecewise_linear(x: float, points):
    """
    points: list of (x, y) sorted by x.
    clamps outside range.
    """
    points = sorted(points, key=lambda p: p[0])
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0) if x1 != x0 else 0.0
            return lerp(y0, y1, t)
    return points[-1][1]

def parse_event_target_to_epoch(s: str) -> float:
    import datetime
    if not s:
        now = datetime.datetime.now()
        dt = now.replace(hour=22, minute=0, second=0, microsecond=0)
        return dt.timestamp()
    dt = datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")
    return dt.timestamp()

def open_meteo_current(lat: float, lon: float):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,wind_speed_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
    }
    r = requests.get(url, params=params, timeout=6)
    r.raise_for_status()
    j = r.json()
    cur = j.get("current", {})
    return float(cur.get("temperature_2m", 0.0)), int(cur.get("weather_code", 0)), float(cur.get("wind_speed_10m", 0.0))

def ors_route_minutes(o_lat, o_lon, d_lat, d_lon, api_key: str) -> float:
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {"Authorization": api_key, "Content-Type": "application/json", "Accept": "application/json"}
    body = {"coordinates": [[o_lon, o_lat], [d_lon, d_lat]]}
    r = requests.post(url, json=body, headers=headers, timeout=8)
    r.raise_for_status()
    j = r.json()
    seconds = float(j["features"][0]["properties"]["summary"]["duration"])
    return seconds / 60.0



def wmo_to_kind(wcode: int, wind_mph: float) -> str:
    
    if wcode == 0:
        kind = "sunny"
    elif wcode in (1, 2, 3, 45, 48):
        kind = "cloudy"
    elif wcode in (95, 96, 99):
        kind = "thunder"
    elif wcode in (71, 73, 75, 77, 85, 86):
        kind = "snow"
    elif wcode in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        kind = "rain"
    else:
        kind = "cloudy"

    # wind overrideeeee
    if wind_mph >= 20 and kind not in ("thunder",):
        kind = "wind"

    # severe overrideeee
    if wcode in (96, 99):
        kind = "severe"

    return kind

WEATHER_TICKS = {
    "sunny": 95,
    "cloudy": 75,
    "rain": 57,
    "thunder": 45,
    "snow": 30,
    "wind": 20,
    "severe": 10,
}

def weather_value(kind: str) -> int:
    return clamp100(WEATHER_TICKS.get(kind, 75))

def temp_value(temp_f: float) -> int:
   
    return clamp100(100.0 - temp_f)

def water_value(last_water_ts: float) -> int:
    
    hours = (time.time() - last_water_ts) / 3600.0
    if hours >= 4.0:
        return 5
    y = piecewise_linear(hours, [
        (0.0, 95),
        (1.0, 70),
        (2.0, 52),
        (3.0, 25),
        (4.0, 5),
    ])
    return clamp100(y)

def stand_value(stand_ms: float, total_ms: float) -> int:
  
    if total_ms <= 0:
        return 100
    pct = (stand_ms / total_ms) * 100.0
    pct = max(0.0, min(40.0, pct)) 
    y = piecewise_linear(pct, [
        (0.0, 100),
        (25.0, 50),
        (40.0, 10),
    ])
    return clamp100(y)

def event_value(seconds_left: float) -> int:
   
    h = seconds_left / 3600.0
    if h <= 0:
        return 30
    if h >= 12:
        return 90

    y = piecewise_linear(h, [
        (0.0, 30),
        (6.0, 70),
        (8.0, 80),
        (10.0, 85),   # assumed
        (12.0, 90),
    ])
    return clamp100(y)

def commute_value(minutes: float) -> int:

    minutes = max(12.0, min(45.0, minutes))
    y = piecewise_linear(minutes, [
        (12.0, 100),
        (18.0, 65),
        (45.0, 30),
    ])
    return clamp100(y)


def event_short(seconds_left: float) -> str:
    if seconds_left <= 0:
        return "NOW"
    mins = seconds_left / 60.0
    if mins < 60:
        return f"{int(round(mins))}m"
    hrs = mins / 60.0
    return f"{hrs:.1f}h"

def send_update(ser: serial.Serial, vals):
    
    msg = "U," + ",".join(str(clamp100(v)) for v in vals) + "\n"
    ser.write(msg.encode("utf-8"))

def main():
    if not PORT:
        raise SystemExit("Set SERIAL_PORT in .env (example: /dev/cu.usbmodemXXXX)")
    if not ORS_API_KEY:
        print("Warning: ORS_API_KEY missing. Commute gauge will hold last value.")

    event_target_epoch = parse_event_target_to_epoch(EVENT_TARGET)

    last_water_ts = time.time()

    standing = False
    stand_ms = 0.0
    total_ms = 0.0
    last_state_ts = time.time()

    temp_f, wcode, wind_mph = 50.0, 3, 0.0
    commute_min = 18.0

    last_weather_poll = 0.0
    last_traffic_poll = 0.0
    last_print = 0.0

    with serial.Serial(PORT, BAUD, timeout=0.05) as ser:
        time.sleep(2)

        while True:
            now = time.time()

            # accumulate sit/stand time
            dt = now - last_state_ts
            if dt > 0:
                total_ms += dt * 1000.0
                if standing:
                    stand_ms += dt * 1000.0
            last_state_ts = now

        #drain
            while ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    break
                if line == "B,WATER":
                    last_water_ts = time.time()
                    print("WATER RESET")
                elif line.startswith("S,"):
                    standing = line.endswith("1")
                    print("STAND STATE =", "STAND" if standing else "SIT")

            # poll weather
            if now - last_weather_poll > WEATHER_POLL_SEC:
                try:
                    temp_f, wcode, wind_mph = open_meteo_current(HOME_LAT, HOME_LON)
                except Exception:
                    pass
                last_weather_poll = now

            # poll commute
            if now - last_traffic_poll > TRAFFIC_POLL_SEC:
                if ORS_API_KEY:
                    try:
                        commute_min = ors_route_minutes(HOME_LAT, HOME_LON, DEST_LAT, DEST_LON, ORS_API_KEY)
                    except Exception:
                        pass
                last_traffic_poll = now

            kind = wmo_to_kind(wcode, wind_mph)

            g_weather = weather_value(kind)
            g_temp    = temp_value(temp_f)
            g_water   = water_value(last_water_ts)
            g_stand   = stand_value(stand_ms, total_ms)

            seconds_left = event_target_epoch - now
            g_event   = event_value(seconds_left)
            g_comm    = commute_value(commute_min)

            vals = [g_weather, g_temp, g_water, g_stand, g_event, g_comm]
            send_update(ser, vals)

            if now - last_print >= PRINT_EVERY_SEC:
                water_h = min(4.0, (time.time() - last_water_ts) / 3600.0)
                stand_pct = 0.0 if total_ms <= 0 else (stand_ms / total_ms) * 100.0

                print(
                    f"WX {kind} -> {g_weather} | "
                    f"TEMP {temp_f:.0f}F -> {g_temp} | "
                    f"WATER {water_h:.2f}h -> {g_water} | "
                    f"STAND {stand_pct:.1f}% -> {g_stand} | "
                    f"EVENT {event_short(seconds_left)} -> {g_event} | "
                    f"COMMUTE {commute_min:.0f}m -> {g_comm}"
                )
                last_print = now

            time.sleep(0.2)

if __name__ == "__main__":
    main()
