"""Local showroom map for the live ESP32 feed.

Listens on 127.0.0.1 only. Reads the sensing server, places one person
from per-board RSSI, and tightens that fix with saved spots. Layout and
fingerprints stay in showroom-data/ and are not radio recordings of a
whole session.

Papers claim sub-meter error after a dense labeled grid. This program
does not print that number. The ring on the map is uncertainty, not a
measured error for this room.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import showroom_zones

HOST = "127.0.0.1"
PORT = 3010
SENSING_URL = "http://127.0.0.1:3000/api/v1/sensing/latest"
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "showroom-data"
UI_DIR = ROOT / "ui" / "showroom"
LAYOUT_PATH = DATA_DIR / "layout.json"
PRINTS_PATH = DATA_DIR / "fingerprints.json"

HEAT_COLS = 40
HEAT_ROWS = 30
ENTRY_DWELL_S = 2.0
SMOOTH_WALK = 0.16
SMOOTH_STILL = 0.07


def default_layout() -> dict:
    return {
        "room": {"width_m": 6.0, "depth_m": 4.0},
        "sensors": [
            {"id": 1, "x": 0.4, "y": 2.0},
            {"id": 2, "x": 5.6, "y": 2.0},
        ],
        "shelves": [],
        "zones": [],
        "router": None,
    }


def empty_prints() -> dict:
    return {"spots": [], "standing": None, "sitting": None}


def _num(value, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be a number")
    return out


def validate_layout(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("layout must be an object")
    room = data.get("room")
    if not isinstance(room, dict):
        raise ValueError("room is required")
    width = _num(room.get("width_m"), "width")
    depth = _num(room.get("depth_m"), "depth")
    if not (1.0 <= width <= 40.0 and 1.0 <= depth <= 40.0):
        raise ValueError("room size must be between 1 and 40 meters")

    raw_sensors = data.get("sensors")
    if not isinstance(raw_sensors, list) or not 1 <= len(raw_sensors) <= 8:
        raise ValueError("place between 1 and 8 sensors")
    sensors = []
    seen = set()
    for sensor in raw_sensors:
        if not isinstance(sensor, dict):
            raise ValueError("sensor must be an object")
        sid = int(_num(sensor.get("id"), "sensor id"))
        if sid < 1 or sid > 16 or sid in seen:
            raise ValueError("sensor ids must be unique numbers from 1 to 16")
        seen.add(sid)
        x = min(width, max(0.0, _num(sensor.get("x"), "sensor x")))
        y = min(depth, max(0.0, _num(sensor.get("y"), "sensor y")))
        sensors.append({"id": sid, "x": round(x, 3), "y": round(y, 3)})

    raw_shelves = data.get("shelves") or []
    if not isinstance(raw_shelves, list) or len(raw_shelves) > 12:
        raise ValueError("use at most 12 shelves")
    shelves = []
    shelf_ids = set()
    for index, shelf in enumerate(raw_shelves):
        if not isinstance(shelf, dict):
            raise ValueError("shelf must be an object")
        sid = str(shelf.get("id") or f"shelf-{index + 1}")
        if not sid.replace("-", "").replace("_", "").isalnum() or len(sid) > 32:
            raise ValueError("shelf id must be short letters, numbers, _ or -")
        if sid in shelf_ids:
            raise ValueError("shelf ids must be unique")
        shelf_ids.add(sid)
        name = str(shelf.get("name") or "Shelf").strip()[:40]
        if not name:
            raise ValueError("shelf name is required")
        x = _num(shelf.get("x"), "shelf x")
        y = _num(shelf.get("y"), "shelf y")
        w = _num(shelf.get("w"), "shelf width")
        h = _num(shelf.get("h"), "shelf depth")
        if w <= 0 or h <= 0:
            raise ValueError("shelf size must be positive")
        x = min(max(0.0, x), width)
        y = min(max(0.0, y), depth)
        w = min(w, width - x)
        h = min(h, depth - y)
        if w < 0.25 or h < 0.25:
            raise ValueError("shelf must be at least 0.25 m on each side")
        shelves.append({
            "id": sid,
            "name": name,
            "x": round(x, 3),
            "y": round(y, 3),
            "w": round(w, 3),
            "h": round(h, 3),
        })
    zones = []
    zone_ids = set()
    raw_zones = data.get("zones") or []
    if not isinstance(raw_zones, list) or len(raw_zones) > 12:
        raise ValueError("use at most 12 zones")
    for index, zone in enumerate(raw_zones):
        if not isinstance(zone, dict):
            raise ValueError("zone must be an object")
        zid = str(zone.get("id") or f"zone-{index + 1}")
        if not zid.replace("-", "").replace("_", "").isalnum() or len(zid) > 32:
            raise ValueError("zone id must be short letters, numbers, _ or -")
        if zid in zone_ids:
            raise ValueError("zone ids must be unique")
        zone_ids.add(zid)
        kind = str(zone.get("kind") or "shelf")
        if kind not in ("shelf", "walkway"):
            raise ValueError("zone kind must be shelf or walkway")
        name = str(zone.get("name") or "Zone").strip()[:40]
        if not name:
            raise ValueError("zone name is required")
        x = min(max(0.0, _num(zone.get("x"), "zone x")), width)
        y = min(max(0.0, _num(zone.get("y"), "zone y")), depth)
        w = min(_num(zone.get("w"), "zone width"), width - x)
        h = min(_num(zone.get("h"), "zone depth"), depth - y)
        if w < 0.25 or h < 0.25:
            raise ValueError("zone must be at least 0.25 m on each side")
        zones.append({
            "id": zid,
            "name": name,
            "kind": kind,
            "x": round(x, 3),
            "y": round(y, 3),
            "w": round(w, 3),
            "h": round(h, 3),
        })
    router = None
    raw_router = data.get("router")
    if isinstance(raw_router, dict) and raw_router.get("x") is not None and raw_router.get("y") is not None:
        rx = _num(raw_router.get("x"), "router x")
        ry = _num(raw_router.get("y"), "router y")
        if not (-30.0 <= rx <= width + 30.0 and -30.0 <= ry <= depth + 30.0):
            raise ValueError("place the router within 30 m of the room")
        router = {"x": round(rx, 3), "y": round(ry, 3)}
    return {
        "room": {"width_m": round(width, 3), "depth_m": round(depth, 3)},
        "sensors": sensors,
        "shelves": shelves,
        "zones": zones,
        "router": router,
    }


def sensing_places(layout: dict) -> list[dict]:
    """Shelves are the places a person stands. Extra zones are added after them."""
    places = []
    seen = set()
    for source in (layout.get("shelves") or []), (layout.get("zones") or []):
        for item in source:
            if not isinstance(item, dict) or item.get("id") in seen:
                continue
            seen.add(item["id"])
            places.append({**item, "kind": item.get("kind") or "shelf"})
    return places


def parse_nodes(payload: dict) -> list[dict]:
    raw = payload.get("node_features") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    nodes = []
    for item in raw:
        if not isinstance(item, dict) or item.get("node_id") is None:
            continue
        feats = item.get("features") if isinstance(item.get("features"), dict) else {}
        cls = item.get("classification") if isinstance(item.get("classification"), dict) else {}
        try:
            rssi = float(item.get("rssi_dbm") if item.get("rssi_dbm") is not None else feats.get("mean_rssi"))
        except (TypeError, ValueError):
            rssi = -70.0
        if not math.isfinite(rssi):
            rssi = -70.0
        level = str(cls.get("motion_level") or "absent")
        last_seen = item.get("last_seen_ms")
        stale = bool(item.get("stale")) or (isinstance(last_seen, (int, float)) and last_seen > 4000)
        nodes.append({
            "id": int(item["node_id"]),
            "rssi": rssi,
            "motion": float(feats.get("motion_band_power") or 0.0),
            "variance": float(feats.get("variance") or 0.0),
            "breathing": float(feats.get("breathing_band_power") or 0.0),
            "level": level,
            "presence": bool(cls.get("presence")) and level != "absent",
            "stale": stale,
        })
    nodes.sort(key=lambda node: node["id"])
    return nodes


def average_rssi(samples: list[list[dict]]) -> dict[str, float]:
    buckets: dict[int, list[float]] = {}
    for sample in samples:
        for node in sample:
            if node["stale"]:
                continue
            buckets.setdefault(node["id"], []).append(node["rssi"])
    return {
        str(node_id): round(sum(values) / len(values), 2)
        for node_id, values in buckets.items()
        if len(values) >= 5
    }


def pose_vector(nodes: list[dict]) -> list[float] | None:
    live = [node for node in nodes if not node["stale"]]
    if not live:
        return None
    count = float(len(live))
    return [
        sum(node["motion"] for node in live) / count,
        sum(node["variance"] for node in live) / count,
        sum(node["breathing"] for node in live) / count,
    ]


def average_pose(samples: list[list[dict]]) -> list[float] | None:
    vectors = [vector for vector in (pose_vector(sample) for sample in samples) if vector is not None]
    if len(vectors) < 5:
        return None
    count = float(len(vectors))
    return [round(sum(vector[i] for vector in vectors) / count, 3) for i in range(3)]


def _rssi_map(stored: dict) -> dict[int, float]:
    out = {}
    for key, value in stored.items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def locate(layout: dict, prints: dict, nodes: list[dict]) -> dict | None:
    """Place one person. RSSI pulls toward the louder board. Four saved
    spots let the fix leave the line between the boards."""
    by_id = {node["id"]: node for node in nodes if not node["stale"]}
    placed = []
    for sensor in layout["sensors"]:
        node = by_id.get(int(sensor["id"]))
        if node is None:
            continue
        weight = 10 ** (node["rssi"] / 20.0)
        placed.append((float(sensor["x"]), float(sensor["y"]), weight, node))
    if not placed:
        return None

    weight_sum = sum(item[2] for item in placed) or 1.0
    rssi_x = sum(item[0] * item[2] for item in placed) / weight_sum
    rssi_y = sum(item[1] * item[2] for item in placed) / weight_sum
    if len(placed) >= 2:
        hi = max(item[2] for item in placed)
        lo = min(item[2] for item in placed) or 1e-12
        db = 20.0 * math.log10(hi / lo)
        coarse_conf = min(0.45, 0.18 + max(0.0, db) / 40.0)
    else:
        coarse_conf = 0.2

    feat = {node["id"]: node["rssi"] for _, _, _, node in placed}
    spots = [spot for spot in prints.get("spots") or [] if isinstance(spot, dict)]
    neighbors = []
    for spot in spots:
        stored = _rssi_map(spot.get("rssi") or {})
        keys = set(feat) & set(stored)
        if len(keys) < 2:
            continue
        dist = math.sqrt(sum((feat[key] - stored[key]) ** 2 for key in keys) / len(keys))
        neighbors.append((dist, float(spot["x"]), float(spot["y"])))
    neighbors.sort(key=lambda item: item[0])

    if len(spots) >= 4 and neighbors:
        chosen = neighbors[:3]
        denom = sum(1.0 / (item[0] + 0.5) for item in chosen)
        knn_x = sum(item[1] / (item[0] + 0.5) for item in chosen) / denom
        knn_y = sum(item[2] / (item[0] + 0.5) for item in chosen) / denom
        x = 0.7 * knn_x + 0.3 * rssi_x
        y = 0.7 * knn_y + 0.3 * rssi_y
        nearest = chosen[0][0]
        confidence = max(0.25, min(0.85, 1.0 - nearest / 20.0))
        mode = "fingerprint"
    else:
        x, y = rssi_x, rssi_y
        confidence = coarse_conf
        mode = "coarse"

    room = layout["room"]
    return {
        "x": min(float(room["width_m"]), max(0.0, x)),
        "y": min(float(room["depth_m"]), max(0.0, y)),
        "mode": mode,
        "confidence": confidence,
        "radius_m": round(1.7 - 1.3 * confidence, 3),
    }


def point_in_shelf(x: float, y: float, shelf: dict) -> bool:
    return (
        shelf["x"] <= x <= shelf["x"] + shelf["w"]
        and shelf["y"] <= y <= shelf["y"] + shelf["h"]
    )


def motion_label(nodes: list[dict], prints: dict, walking: bool) -> str:
    if walking:
        return "walking"
    standing = prints.get("standing")
    sitting = prints.get("sitting")
    current = pose_vector(nodes)
    if (
        isinstance(standing, list)
        and isinstance(sitting, list)
        and current is not None
        and len(standing) == 3
        and len(sitting) == 3
    ):
        def dist(sample: list) -> float:
            return math.sqrt(sum(
                ((current[i] - float(sample[i])) / (abs(float(sample[i])) + 1.0)) ** 2
                for i in range(3)
            ))

        if dist(sitting) < dist(standing) * 0.75:
            return "sitting"
    return "still"


class Tracker:
    def __init__(self) -> None:
        self.x: float | None = None
        self.y: float | None = None
        self.walk = 0.0
        self.walking = False
        self.present_hold = 0.0
        self.absent_for = 0.0
        self.trail: list[list[float]] = []
        self.trail_timer = 0.0
        self.heat = [0.0] * (HEAT_COLS * HEAT_ROWS)
        self.shelves: dict[str, dict] = {}

    def reset_stats(self) -> None:
        self.trail.clear()
        self.heat = [0.0] * (HEAT_COLS * HEAT_ROWS)
        for state in self.shelves.values():
            state["entries"] = 0
            state["dwell"] = 0.0
            state["inside_for"] = 0.0
            state["outside_for"] = 0.0
            state["latched"] = False

    def reset_heat(self) -> None:
        self.heat = [0.0] * (HEAT_COLS * HEAT_ROWS)

    def update(self, layout: dict, prints: dict, nodes: list[dict] | None, dt: float) -> dict:
        dt = min(1.0, max(0.0, dt))
        if dt:
            decay = math.exp(-dt / 45.0)
            self.heat = [value * decay for value in self.heat]

        live_nodes = [node for node in (nodes or []) if not node["stale"]]
        any_present = any(node["presence"] for node in live_nodes)
        if any_present:
            self.present_hold = 1.0
        else:
            self.present_hold = max(0.0, self.present_hold - dt / 2.5)
        present = any_present or self.present_hold > 0.2

        moving = any(
            node["presence"] and node["level"] in ("present_moving", "active")
            for node in live_nodes
        )
        if not present:
            self.walk += (0.0 - self.walk) * 0.08
        elif moving:
            self.walk += (1.0 - self.walk) * 0.08
        else:
            self.walk += (0.0 - self.walk) * 0.05
        self.walking = self.walk > (0.32 if self.walking else 0.62)

        fix = locate(layout, prints, live_nodes) if present and nodes is not None else None
        if fix is not None:
            alpha = SMOOTH_WALK if self.walking else SMOOTH_STILL
            if self.x is None:
                self.x, self.y = fix["x"], fix["y"]
            else:
                self.x += (fix["x"] - self.x) * alpha
                self.y += (fix["y"] - self.y) * alpha
            self.absent_for = 0.0
            self.trail_timer += dt
            if self.trail_timer >= 0.4:
                self.trail.append([round(self.x, 3), round(self.y, 3)])
                self.trail = self.trail[-40:]
                self.trail_timer = 0.0
            self._add_heat(layout, self.x, self.y, dt)
        else:
            self.absent_for += dt
            if self.absent_for > 8.0:
                self.trail.clear()

        motion = motion_label(live_nodes, prints, self.walking) if present and live_nodes else "none"
        shelf_rows = []
        zone = None
        zone_area = None
        for shelf in layout.get("shelves") or []:
            state = self.shelves.setdefault(shelf["id"], {
                "entries": 0,
                "dwell": 0.0,
                "inside_for": 0.0,
                "outside_for": 0.0,
                "latched": False,
            })
            inside = (
                present
                and self.x is not None
                and point_in_shelf(self.x, self.y or 0.0, shelf)
            )
            if inside:
                state["outside_for"] = 0.0
                state["inside_for"] += dt
                state["dwell"] += dt
                if state["inside_for"] >= ENTRY_DWELL_S and not state["latched"]:
                    state["entries"] += 1
                    state["latched"] = True
                area = shelf["w"] * shelf["h"]
                if zone_area is None or area < zone_area:
                    zone = {"id": shelf["id"], "name": shelf["name"]}
                    zone_area = area
            elif present:
                state["outside_for"] += dt
                if state["outside_for"] >= 1.5:
                    state["inside_for"] = 0.0
                    state["latched"] = False
            shelf_rows.append({
                "id": shelf["id"],
                "name": shelf["name"],
                "entries": state["entries"],
                "dwell_s": round(state["dwell"], 1),
                "inside": inside,
            })

        known = {shelf["id"] for shelf in layout.get("shelves") or []}
        for shelf_id in list(self.shelves):
            if shelf_id not in known:
                del self.shelves[shelf_id]

        peak = max(self.heat) if self.heat else 0.0
        scale = peak or 1.0
        return {
            "present": bool(present and self.x is not None),
            "x": None if self.x is None else round(self.x, 3),
            "y": None if self.y is None else round(self.y, 3),
            "zone": zone,
            "motion": motion,
            "confidence": 0.0 if fix is None else round(fix["confidence"], 3),
            "radius_m": 1.7 if fix is None else fix["radius_m"],
            "mode": "none" if fix is None else fix["mode"],
            "trail": list(self.trail),
            "heatmap": {
                "cols": HEAT_COLS,
                "rows": HEAT_ROWS,
                "values": [int(value / scale * 255) for value in self.heat],
            },
            "shelves": shelf_rows,
        }

    def _add_heat(self, layout: dict, x: float, y: float, dt: float) -> None:
        room = layout["room"]
        width = float(room["width_m"]) or 1.0
        depth = float(room["depth_m"]) or 1.0
        fx = x / width * (HEAT_COLS - 1)
        fy = y / depth * (HEAT_ROWS - 1)
        for iy in range(HEAT_ROWS):
            for ix in range(HEAT_COLS):
                dist2 = (ix - fx) ** 2 + (iy - fy) ** 2
                if dist2 > 25:
                    continue
                self.heat[iy * HEAT_COLS + ix] += math.exp(-dist2 / (2 * 1.6 * 1.6)) * dt


def load_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return data if isinstance(data, dict) else fallback


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _blank_snapshot() -> dict:
    return {
        "present": False,
        "x": None,
        "y": None,
        "zone": None,
        "motion": "none",
        "confidence": 0.0,
        "radius_m": 1.7,
        "mode": "none",
        "trail": [],
        "heatmap": {
            "cols": HEAT_COLS,
            "rows": HEAT_ROWS,
            "values": [0] * (HEAT_COLS * HEAT_ROWS),
        },
        "shelves": [],
        "nodes": [],
    }


class Engine:
    def __init__(self) -> None:
        raw_layout = load_json(LAYOUT_PATH, default_layout())
        try:
            self.layout = validate_layout(raw_layout)
        except ValueError:
            self.layout = default_layout()
        self.prints = load_json(PRINTS_PATH, empty_prints())
        if not isinstance(self.prints.get("spots"), list):
            self.prints = empty_prints()
        self.prints.setdefault("standing", None)
        self.prints.setdefault("sitting", None)
        self.tracker = Tracker()
        self.capture: dict | None = None
        self.sensing_ok = False
        self._last = 0.0
        self._lock = threading.Lock()
        self.snapshot = _blank_snapshot()
        self.zones = showroom_zones.ZoneApp(DATA_DIR)

    def poll_once(self) -> None:
        nodes = None
        ok = False
        try:
            with urllib.request.urlopen(SENSING_URL, timeout=1.5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            nodes = parse_nodes(payload)
            ok = True
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            ok = False
        now = time.monotonic()
        with self._lock:
            dt = 0.2 if self._last == 0.0 else min(1.0, max(0.0, now - self._last))
            self._last = now
            self.sensing_ok = ok
            if ok and nodes is not None:
                self._maybe_capture(nodes, now)
            tracked = self.tracker.update(self.layout, self.prints, nodes if ok else None, dt)
            tracked["nodes"] = [
                {
                    "id": node["id"],
                    "rssi": round(node["rssi"], 1),
                    "presence": node["presence"],
                    "stale": node["stale"],
                    "level": node["level"],
                }
                for node in (nodes or [])
            ]
            self.snapshot = tracked
            zone_layout = sensing_places(self.layout)
        self.zones.tick(zone_layout)

    def _maybe_capture(self, nodes: list[dict], now: float) -> None:
        cap = self.capture
        if not cap or cap.get("done"):
            return
        if now < cap["sample_at"]:
            return
        if now < cap["until"]:
            cap["samples"].append(nodes)
            return
        self._finish_capture()

    def _finish_capture(self) -> None:
        cap = self.capture
        if cap is None or cap.get("done"):
            return
        try:
            if cap["kind"] == "spot":
                rssi = average_rssi(cap["samples"])
                if len(rssi) < 2:
                    raise ValueError("both boards must be heard while you stand on the spot")
                self.prints.setdefault("spots", []).append({
                    "x": cap["x"],
                    "y": cap["y"],
                    "rssi": rssi,
                })
            else:
                vector = average_pose(cap["samples"])
                if vector is None:
                    raise ValueError("not enough radio samples")
                self.prints[cap["kind"]] = vector
            save_json(PRINTS_PATH, self.prints)
            cap["error"] = None
        except ValueError as exc:
            cap["error"] = str(exc)
        cap["done"] = True

    def set_layout(self, layout: dict) -> dict:
        layout = validate_layout(layout)
        with self._lock:
            if self.layout["room"] != layout["room"]:
                self.tracker.reset_heat()
            self.layout = layout
            save_json(LAYOUT_PATH, layout)
            return layout

    def reset_session(self) -> None:
        with self._lock:
            self.tracker.reset_stats()
        self.zones.reset_counts()

    def clear_prints(self) -> None:
        with self._lock:
            self.prints = empty_prints()
            self.capture = None
            save_json(PRINTS_PATH, self.prints)

    def remove_print(self, kind: str, index: int | None) -> None:
        with self._lock:
            if kind == "spot":
                spots = list(self.prints.get("spots") or [])
                if index is None or index < 0 or index >= len(spots):
                    raise ValueError("spot not found")
                del spots[index]
                self.prints["spots"] = spots
            elif kind in ("standing", "sitting"):
                self.prints[kind] = None
            else:
                raise ValueError("unknown sample")
            save_json(PRINTS_PATH, self.prints)

    def start_capture(self, kind: str, x, y, seconds: float) -> dict:
        if kind not in ("spot", "standing", "sitting"):
            raise ValueError("kind must be spot, standing, or sitting")
        seconds = min(20.0, max(3.0, float(seconds or 8)))
        lead_s = 15.0
        with self._lock:
            if not self.sensing_ok:
                raise RuntimeError("sensing is offline")
            now = time.monotonic()
            if self.capture and not self.capture.get("done") and now < self.capture["until"]:
                raise RuntimeError("already recording")
            spot_x = spot_y = None
            if kind == "spot":
                if x is None or y is None:
                    raise ValueError("click the floor before recording a spot")
                room = self.layout["room"]
                spot_x = float(x)
                spot_y = float(y)
                if not (0 <= spot_x <= room["width_m"] and 0 <= spot_y <= room["depth_m"]):
                    raise ValueError("that spot is outside the room")
                spot_x = round(spot_x, 3)
                spot_y = round(spot_y, 3)
            self.capture = {
                "kind": kind,
                "x": spot_x,
                "y": spot_y,
                "sample_at": now + lead_s,
                "until": now + lead_s + seconds,
                "samples": [],
                "error": None,
                "done": False,
            }
            return {"status": "recording", "kind": kind, "seconds": seconds, "lead_s": lead_s}

    def public_snapshot(self) -> dict:
        with self._lock:
            snap = dict(self.snapshot)
            now = time.monotonic()
            snap["sensing"] = "ok" if self.sensing_ok else "down"
            snap["fingerprint_count"] = len(self.prints.get("spots") or [])
            snap["pose_samples"] = {
                "standing": isinstance(self.prints.get("standing"), list),
                "sitting": isinstance(self.prints.get("sitting"), list),
            }
            snap["spots"] = [
                {"x": spot.get("x"), "y": spot.get("y")}
                for spot in (self.prints.get("spots") or [])
                if isinstance(spot, dict)
            ]
            snap["room"] = self.layout["room"]
            cap = self.capture
            if not cap:
                snap["capture"] = None
            elif not cap.get("done") and now < cap["until"]:
                walking_there = now < cap.get("sample_at", now)
                snap["capture"] = {
                    "active": True,
                    "phase": "go" if walking_there else "record",
                    "remaining_s": round((cap["sample_at"] if walking_there else cap["until"]) - now, 1),
                    "kind": cap["kind"],
                    "error": None,
                }
            else:
                snap["capture"] = {
                    "active": False,
                    "remaining_s": 0,
                    "kind": cap["kind"],
                    "error": cap.get("error"),
                }
            zone_layout = sensing_places(self.layout)
        zone_public = self.zones.public(zone_layout)
        snap["zone_view"] = {
            "enabled": zone_public["zone_mode"],
            "uncertain": zone_public["uncertain"],
            "zones": zone_public["zones"],
            "route": zone_public["route"],
            "eval": zone_public["eval"],
            "rates": zone_public["rates"],
            "window_s": zone_public["window_s"],
            "teach": zone_public["teach"],
            "recordings": zone_public["recordings"],
        }
        if zone_public["zone_mode"]:
            snap["x"] = zone_public["x"]
            snap["y"] = zone_public["y"]
            snap["zone"] = zone_public["zone"]
            snap["present"] = zone_public["zone"] is not None
            snap["motion"] = zone_public["motion"]
            snap["mode"] = "zone"
        else:
            snap["x"] = None
            snap["y"] = None
            snap["zone"] = None
            snap["mode"] = "unteach"
            if zone_public["motion"] != "none":
                snap["motion"] = zone_public["motion"]
        return snap


ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/showroom/live":
            self._json(200, ENGINE.public_snapshot())
        elif path == "/api/showroom/layout":
            with ENGINE._lock:
                self._json(200, ENGINE.layout)
        elif path == "/api/showroom/fingerprints":
            with ENGINE._lock:
                self._json(200, {
                    "spots": [
                        {"x": spot.get("x"), "y": spot.get("y")}
                        for spot in ENGINE.prints.get("spots") or []
                        if isinstance(spot, dict)
                    ],
                    "standing": isinstance(ENGINE.prints.get("standing"), list),
                    "sitting": isinstance(ENGINE.prints.get("sitting"), list),
                })
        elif path in ("/", "/index.html"):
            self._file("index.html")
        elif path == "/showroom.css":
            self._file("showroom.css")
        elif path == "/showroom.js":
            self._file("showroom.js")
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if urlparse(self.path).path != "/api/showroom/layout":
            self._json(404, {"error": "not found"})
            return
        try:
            self._json(200, ENGINE.set_layout(self._read_json()))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/showroom/calibrate":
                body = self._read_json()
                self._json(200, ENGINE.start_capture(
                    str(body.get("kind") or ""),
                    body.get("x"),
                    body.get("y"),
                    float(body.get("seconds") or 8),
                ))
            elif path == "/api/showroom/teach":
                body = self._read_json()
                label = str(body.get("label") or body.get("zone_id") or "")
                self._json(200, ENGINE.zones.start_teach(label, str(body.get("session") or "A")))
            elif path == "/api/showroom/eval":
                self._json(200, ENGINE.zones.retrain())
            elif path == "/api/showroom/session/reset":
                ENGINE.reset_session()
                self._json(200, {"status": "reset"})
            elif path == "/api/showroom/fingerprints/remove":
                body = self._read_json()
                index = body.get("index")
                ENGINE.remove_print(
                    str(body.get("kind") or ""),
                    None if index is None else int(index),
                )
                self._json(200, {"status": "removed"})
            else:
                self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            code = 503 if "offline" in str(exc) else 409
            self._json(code, {"error": str(exc)})

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/api/showroom/fingerprints":
            self._json(404, {"error": "not found"})
            return
        ENGINE.clear_prints()
        self._json(200, {"status": "cleared"})

    def log_message(self, fmt: str, *args) -> None:
        if len(args) > 1 and str(args[1]) != "200":
            super().log_message(fmt, *args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 65536:
            raise ValueError("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        return data

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name: str) -> None:
        path = (UI_DIR / name).resolve()
        if path.parent != UI_DIR.resolve() or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        kind = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _poll_loop() -> None:
    while True:
        started = time.monotonic()
        ENGINE.poll_once()
        time.sleep(max(0.05, 0.2 - (time.monotonic() - started)))


def serve() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENGINE.zones.start()
    threading.Thread(target=_poll_loop, name="showroom-poll", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"showroom: http://{HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def self_check() -> None:
    layout = validate_layout({
        "room": {"width_m": 6, "depth_m": 4},
        "sensors": [{"id": 1, "x": 0.4, "y": 2}, {"id": 2, "x": 5.6, "y": 2}],
        "shelves": [{"id": "center", "name": "Center", "x": 2.5, "y": 1.5, "w": 1, "h": 1}],
    })

    def nodes(rssi1: float, rssi2: float, level: str = "present_still") -> list[dict]:
        return [
            {"id": 1, "rssi": rssi1, "motion": 10.0, "variance": 1.0, "breathing": 0.2,
             "level": level, "presence": True, "stale": False},
            {"id": 2, "rssi": rssi2, "motion": 10.0, "variance": 1.0, "breathing": 0.2,
             "level": level, "presence": True, "stale": False},
        ]

    absent = [
        {"id": 1, "rssi": -50, "motion": 0, "variance": 0, "breathing": 0,
         "level": "absent", "presence": False, "stale": False},
        {"id": 2, "rssi": -50, "motion": 0, "variance": 0, "breathing": 0,
         "level": "absent", "presence": False, "stale": False},
    ]

    mid = locate(layout, empty_prints(), nodes(-50, -50))
    assert mid is not None and abs(mid["x"] - 3.0) < 0.05 and mid["mode"] == "coarse"
    left = locate(layout, empty_prints(), nodes(-40, -70))
    assert left is not None and left["x"] < 1.2

    prints = {"spots": [
        {"x": 1.0, "y": 3.2, "rssi": {"1": -48, "2": -62}},
        {"x": 1.0, "y": 0.8, "rssi": {"1": -40, "2": -70}},
        {"x": 5.0, "y": 3.2, "rssi": {"1": -70, "2": -40}},
        {"x": 5.0, "y": 0.8, "rssi": {"1": -65, "2": -45}},
    ], "standing": None, "sitting": None}
    learned = locate(layout, prints, nodes(-48, -62))
    assert learned is not None and learned["mode"] == "fingerprint"
    assert abs(learned["y"] - 2.0) > 0.4

    tracker = Tracker()
    view = {}
    for _ in range(12):
        view = tracker.update(layout, empty_prints(), nodes(-50, -50), 0.25)
    assert view["shelves"][0]["entries"] == 1
    assert view["zone"]["name"] == "Center"
    for _ in range(8):
        view = tracker.update(layout, empty_prints(), nodes(-50, -50), 0.25)
    assert view["shelves"][0]["entries"] == 1
    for _ in range(16):
        view = tracker.update(layout, empty_prints(), nodes(-40, -70), 0.25)
    assert view["zone"] is None
    assert view["shelves"][0]["entries"] == 1
    for _ in range(24):
        view = tracker.update(layout, empty_prints(), nodes(-50, -50), 0.25)
    assert view["shelves"][0]["entries"] == 2

    for _ in range(4):
        view = tracker.update(layout, empty_prints(), absent, 1.0)
    paused = view["shelves"][0]["dwell_s"]
    view = tracker.update(layout, empty_prints(), absent, 1.0)
    assert view["present"] is False
    assert view["shelves"][0]["dwell_s"] == paused

    walking = Tracker()
    walked = {}
    for _ in range(20):
        walked = walking.update(layout, empty_prints(), nodes(-50, -55, "active"), 0.2)
    assert walked["motion"] == "walking"

    try:
        validate_layout({"room": {"width_m": 100, "depth_m": 4}, "sensors": [{"id": 1, "x": 1, "y": 1}]})
        raise AssertionError("large room should fail")
    except ValueError:
        pass
    print("showroom check: PASS")


def main() -> int:
    if "--check" in sys.argv:
        self_check()
        showroom_zones.self_check()
        return 0
    if "--eval-rssi" in sys.argv:
        print(json.dumps(showroom_zones.eval_rssi(PRINTS_PATH), indent=2))
        return 0
    if "--eval" in sys.argv:
        app = showroom_zones.ZoneApp(DATA_DIR)
        result = app.eval if app.eval else showroom_zones.evaluate_sessions(app.recordings)
        print(json.dumps(result, indent=2))
        return 0
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
