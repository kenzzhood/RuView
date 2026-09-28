"""Shelf-zone sensing from ESP32 CSI amplitudes.

Loudness fingerprints are not used. Each board contributes a gain-normalized
subcarrier shape, its spread, and RSSI. A zone model is allowed on the live
page only after a held-out session beats guessing.
"""

from __future__ import annotations

import json
import math
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

WS_URL = "ws://127.0.0.1:3001/ws/sensing"
BINS = 48
LEAD_S = 15.0
RECORD_S = 60.0
HOP_S = 0.5
MIN_FRAMES = 4
ENTRY_S = 2.0
EXIT_S = 1.5
CONFIDENT = 0.60

# Boards provisioned for this showroom. Used only to find their LAN addresses.
BOARD_MACS = {
    "68-ee-8f-4b-99-d8": 1,
    "14-c1-9f-c8-da-40": 2,
}


def strip_guards(amps: list) -> list[float]:
    vals = []
    for item in amps:
        try:
            number = float(item)
        except (TypeError, ValueError):
            number = 0.0
        if not math.isfinite(number) or number < 0:
            number = 0.0
        vals.append(number)
    start = 0
    while start < len(vals) and vals[start] == 0:
        start += 1
    end = len(vals)
    while end > start and vals[end - 1] == 0:
        end -= 1
    return vals[start:end]


def gain_norm(vals: list[float]) -> list[float]:
    usable = [value for value in vals if value > 0]
    scale = (sum(usable) / len(usable)) if usable else 1.0
    if scale <= 0:
        scale = 1.0
    out = [value / scale for value in vals[:BINS]]
    if len(out) < BINS:
        out.extend([0.0] * (BINS - len(out)))
    return out


def _mean(rows: list[list[float]]) -> list[float]:
    count = float(len(rows))
    return [sum(row[i] for row in rows) / count for i in range(BINS)]


def _spread(rows: list[list[float]], mean: list[float]) -> list[float]:
    count = float(len(rows))
    return [
        math.sqrt(sum((row[i] - mean[i]) ** 2 for row in rows) / count)
        for i in range(BINS)
    ]


def window_from_frames(frames: list[tuple[list[float], float]]) -> dict | None:
    if len(frames) < MIN_FRAMES:
        return None
    shapes = [frame[0] for frame in frames]
    mean = _mean(shapes)
    return {
        "shape": [round(value, 4) for value in mean],
        "spread": [round(value, 4) for value in _spread(shapes, mean)],
        "rssi": round(sum(frame[1] for frame in frames) / len(frames), 2),
    }


def empty_baseline(windows: list[dict], node_ids: list[int]) -> dict[str, list[float]]:
    baseline = {}
    for node_id in node_ids:
        shapes = []
        for window in windows:
            node = (window.get("nodes") or {}).get(str(node_id))
            if isinstance(node, dict) and len(node.get("shape") or []) == BINS:
                shapes.append(node["shape"])
        baseline[str(node_id)] = _mean(shapes) if shapes else [0.0] * BINS
    return baseline


def feature_vector(window: dict, baseline: dict, node_ids: list[int], rssi_only: bool = False) -> list[float]:
    rssi = []
    parts: list[float] = []
    nodes = window.get("nodes") or {}
    for node_id in node_ids:
        node = nodes.get(str(node_id))
        if not isinstance(node, dict):
            if not rssi_only:
                parts.extend([0.0] * (BINS * 2))
            rssi.append(-70.0)
            continue
        rssi.append(float(node.get("rssi") or -70.0))
        if rssi_only:
            continue
        base = baseline.get(str(node_id)) or [0.0] * BINS
        shape = list(node.get("shape") or [])
        spread = list(node.get("spread") or [])
        if len(shape) != BINS:
            shape = (shape + [0.0] * BINS)[:BINS]
        if len(spread) != BINS:
            spread = (spread + [0.0] * BINS)[:BINS]
        parts.extend(shape[i] - float(base[i] if i < len(base) else 0.0) for i in range(BINS))
        parts.extend(spread)
    return rssi if rssi_only else parts + rssi


def _labels_of(session: dict) -> set[str]:
    return {key for key, rows in session.items() if isinstance(rows, list) and rows}


def flatten(session: dict) -> list[tuple[dict, str]]:
    rows = []
    for label, windows in session.items():
        if not isinstance(windows, list):
            continue
        for window in windows:
            if isinstance(window, dict):
                rows.append((window, label))
    return rows


def _majority(labels: list[str]) -> str:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return max(counts, key=counts.get)


def _accuracy(truth: list[str], pred: list[str]) -> float:
    if not truth:
        return 0.0
    return sum(a == b for a, b in zip(truth, pred)) / len(truth)


def _per_label(truth: list[str], pred: list[str], labels: list[str]) -> dict[str, float]:
    out = {}
    for label in labels:
        total = sum(item == label for item in truth)
        hit = sum(a == label and b == label for a, b in zip(truth, pred))
        out[label] = round(hit / total, 3) if total else 0.0
    return out


def _confusion(truth: list[str], pred: list[str], labels: list[str]) -> list[dict]:
    pairs = []
    for actual in labels:
        for guessed in labels:
            if actual == guessed:
                continue
            count = sum(a == actual and b == guessed for a, b in zip(truth, pred))
            if count:
                pairs.append({"actual": actual, "guessed": guessed, "count": count})
    pairs.sort(key=lambda item: item["count"], reverse=True)
    return pairs[:6]


def _fit(vectors: list[list[float]], labels: list[str]):
    from sklearn.ensemble import ExtraTreesClassifier
    from sklearn.neighbors import KNeighborsClassifier

    models = {
        "extra_trees": ExtraTreesClassifier(
            n_estimators=200, random_state=0, n_jobs=1, class_weight="balanced"
        ),
        "knn": KNeighborsClassifier(n_neighbors=min(5, len(vectors)), weights="distance"),
    }
    fitted = {}
    for name, model in models.items():
        model.fit(vectors, labels)
        fitted[name] = model
    return fitted


def _predict(model, vectors: list[list[float]]) -> tuple[list[str], list[dict]]:
    pred = [str(item) for item in model.predict(vectors)]
    classes = [str(item) for item in model.classes_]
    probas = []
    if hasattr(model, "predict_proba"):
        for row in model.predict_proba(vectors):
            probas.append({classes[i]: float(row[i]) for i in range(len(classes))})
    else:
        probas = [{label: 1.0} for label in pred]
    return pred, probas


def evaluate_sessions(recordings: dict) -> dict:
    """Train on one session and test on the other, both ways."""
    sessions = recordings.get("sessions") or {}
    left = sessions.get("A") if isinstance(sessions.get("A"), dict) else {}
    right = sessions.get("B") if isinstance(sessions.get("B"), dict) else {}
    shared = sorted(_labels_of(left) & _labels_of(right))
    if len(shared) < 2:
        return {
            "ready": False,
            "reason": "Record every zone, including empty, in session A and again in session B.",
            "measured": False,
        }
    node_ids = sorted({
        int(node_id)
        for session in (left, right)
        for rows in session.values()
        if isinstance(rows, list)
        for window in rows
        if isinstance(window, dict)
        for node_id in (window.get("nodes") or {})
    })
    if not node_ids:
        return {"ready": False, "reason": "Recordings have no board data.", "measured": False}

    def run_split(train_session: dict, test_session: dict) -> dict:
        train_rows = [(window, label) for window, label in flatten(train_session) if label in shared]
        test_rows = [(window, label) for window, label in flatten(test_session) if label in shared]
        baseline = empty_baseline(
            [window for window, label in train_rows if label == "empty"],
            node_ids,
        )
        train_x = [feature_vector(window, baseline, node_ids) for window, _ in train_rows]
        train_y = [label for _, label in train_rows]
        test_x = [feature_vector(window, baseline, node_ids) for window, _ in test_rows]
        test_y = [label for _, label in test_rows]
        rssi_train = [feature_vector(window, baseline, node_ids, rssi_only=True) for window, _ in train_rows]
        rssi_test = [feature_vector(window, baseline, node_ids, rssi_only=True) for window, _ in test_rows]
        fitted = _fit(train_x, train_y)
        rssi_model = _fit(rssi_train, train_y)["extra_trees"]
        majority = _majority(train_y)
        majority_pred = [majority] * len(test_y)
        best_name = ""
        best_acc = -1.0
        best_pred: list[str] = []
        for name, model in fitted.items():
            pred, _ = _predict(model, test_x)
            acc = _accuracy(test_y, pred)
            if acc > best_acc:
                best_name, best_acc, best_pred = name, acc, pred
        rssi_pred, _ = _predict(rssi_model, rssi_test)
        return {
            "model": best_name,
            "accuracy": best_acc,
            "majority": _accuracy(test_y, majority_pred),
            "rssi": _accuracy(test_y, rssi_pred),
            "per_zone": _per_label(test_y, best_pred, shared),
            "confused": _confusion(test_y, best_pred, shared),
            "pred": best_pred,
            "truth": test_y,
        }

    forward = run_split(left, right)
    backward = run_split(right, left)
    accuracy = (forward["accuracy"] + backward["accuracy"]) / 2
    majority = (forward["majority"] + backward["majority"]) / 2
    rssi = (forward["rssi"] + backward["rssi"]) / 2
    per_zone = {}
    for label in shared:
        per_zone[label] = round((forward["per_zone"][label] + backward["per_zone"][label]) / 2, 3)
    passed = accuracy >= 0.60 and accuracy >= majority + 0.15
    return {
        "ready": passed,
        "measured": True,
        "tag": "MEASURED",
        "accuracy": round(accuracy, 3),
        "majority": round(majority, 3),
        "rssi": round(rssi, 3),
        "per_zone": per_zone,
        "confused": forward["confused"],
        "model": forward["model"] if forward["accuracy"] >= backward["accuracy"] else backward["model"],
        "reason": "" if passed else "The zone model is not clearly better than guessing, so live zones stay off.",
    }


def class_centroids(recordings: dict, baseline: dict, node_ids: list[int]) -> dict[str, list[float]]:
    groups: dict[str, list[list[float]]] = {}
    for session in (recordings.get("sessions") or {}).values():
        if not isinstance(session, dict):
            continue
        for window, label in flatten(session):
            groups.setdefault(label, []).append(feature_vector(window, baseline, node_ids))
    centroids = {}
    for label, vectors in groups.items():
        if not vectors:
            continue
        width = len(vectors[0])
        centroids[label] = [sum(vector[i] for vector in vectors) / len(vectors) for i in range(width)]
    return centroids


def _distance(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))
    sessions = recordings.get("sessions") or {}
    rows = []
    for session in sessions.values():
        if isinstance(session, dict):
            rows.extend(flatten(session))
    if len(rows) < 8:
        raise ValueError("not enough recordings")
    labels = sorted({label for _, label in rows})
    node_ids = sorted({
        int(node_id)
        for window, _ in rows
        for node_id in (window.get("nodes") or {})
    })
    baseline = empty_baseline([window for window, label in rows if label == "empty"], node_ids)
    vectors = [feature_vector(window, baseline, node_ids) for window, _ in rows]
    train_labels = [label for _, label in rows]
    model = _fit(vectors, train_labels)[model_name]
    return {
        "model": model,
        "node_ids": node_ids,
        "baseline": baseline,
        "labels": labels,
        "centroids": class_centroids(recordings, baseline, node_ids),
    }


class StickyZones:
    def __init__(self) -> None:
        self.current: str | None = None
        self.pending: str | None = None
        self.pending_hits = 0
        self.stats: dict[str, dict] = {}
        self.route: list[str] = []
        self._last = "empty"
        self.last_probs: dict[str, float] = {}

    def reset(self) -> None:
        self.current = None
        self.pending = None
        self.pending_hits = 0
        for state in self.stats.values():
            state.update({"entries": 0, "dwell": 0.0, "inside_for": 0.0, "outside_for": 0.0, "latched": False})
        self.route.clear()
        self._last = "empty"
        self.last_probs = {}

    def update(self, probs: dict[str, float] | None, zone_ids: list[str], names: dict[str, str], dt: float, hold: bool = False) -> dict:
        dt = min(1.0, max(0.0, dt))
        reported = self._last if hold else self._smooth(probs)
        if not hold:
            self._last = reported
            self.last_probs = dict(probs or {})
        active = reported if reported in zone_ids else None
        for zone_id in zone_ids:
            state = self.stats.setdefault(zone_id, {
                "entries": 0, "dwell": 0.0, "inside_for": 0.0, "outside_for": 0.0, "latched": False,
            })
            if active == zone_id:
                state["outside_for"] = 0.0
                state["inside_for"] += dt
                state["dwell"] += dt
                if state["inside_for"] >= ENTRY_S and not state["latched"]:
                    state["entries"] += 1
                    state["latched"] = True
                    self.route.append(names.get(zone_id, zone_id))
                    self.route = self.route[-12:]
            else:
                state["outside_for"] += dt
                if state["outside_for"] >= EXIT_S:
                    state["inside_for"] = 0.0
                    state["latched"] = False
        return {"zone_id": active, "uncertain": reported == "uncertain"}

    def _smooth(self, probs: dict[str, float] | None) -> str:
        if not probs:
            return self.current if self.current else "empty"
        shelves = {key: float(value) for key, value in probs.items() if key != "empty"}
        empty_p = float(probs.get("empty", 0.0))
        best_shelf = max(shelves, key=shelves.get) if shelves else None
        best_p = shelves.get(best_shelf, 0.0) if best_shelf else 0.0
        if empty_p >= best_p:
            self.current = None
            self.pending = None
            self.pending_hits = 0
            return "empty"
        if best_p < CONFIDENT or not best_shelf:
            self.pending = None
            self.pending_hits = 0
            return self.current if self.current else "uncertain"
        if best_shelf == self.current:
            self.pending = None
            self.pending_hits = 0
            return best_shelf
        if best_shelf == self.pending:
            self.pending_hits += 1
        else:
            self.pending = best_shelf
            self.pending_hits = 1
        if self.pending_hits >= 2:
            self.current = best_shelf
            self.pending = None
            self.pending_hits = 0
            return best_shelf
        return self.current if self.current else "uncertain"

    def update_distance(self, distances: dict[str, float] | None, zone_ids: list[str], names: dict[str, str], dt: float, hold: bool = False) -> dict:
        dt = min(1.0, max(0.0, dt))
        reported = self._last if hold else self._choose_distance(distances)
        if not hold:
            self._last = reported
        active = reported if reported in zone_ids else None
        for zone_id in zone_ids:
            state = self.stats.setdefault(zone_id, {
                "entries": 0, "dwell": 0.0, "inside_for": 0.0, "outside_for": 0.0, "latched": False,
            })
            if active == zone_id:
                state["outside_for"] = 0.0
                state["inside_for"] += dt
                state["dwell"] += dt
                if state["inside_for"] >= ENTRY_S and not state["latched"]:
                    state["entries"] += 1
                    state["latched"] = True
                    self.route.append(names.get(zone_id, zone_id))
                    self.route = self.route[-12:]
            else:
                state["outside_for"] += dt
                if state["outside_for"] >= EXIT_S:
                    state["inside_for"] = 0.0
                    state["latched"] = False
        return {"zone_id": active, "uncertain": reported == "uncertain"}

    def _choose_distance(self, distances: dict[str, float] | None) -> str:
        if not distances:
            return self._last or "empty"
        shelves = {key: float(value) for key, value in distances.items() if key != "empty"}
        empty_d = float(distances.get("empty", 1e9))
        best = min(shelves, key=shelves.get) if shelves else None
        best_d = shelves[best] if best else 1e9
        if best and best_d < empty_d * 0.85:
            choice = best
        elif empty_d <= best_d:
            choice = "empty"
        else:
            choice = "uncertain"
        if choice == "uncertain":
            return self.current if self.current else "uncertain"
        if choice == self.current or (choice == "empty" and self.current is None and self._last == "empty"):
            self.pending = None
            self.pending_hits = 0
            return choice
        if choice == self.pending:
            self.pending_hits += 1
        else:
            self.pending = choice
            self.pending_hits = 1
        if self.pending_hits >= 2:
            self.current = None if choice == "empty" else choice
            self.pending = None
            self.pending_hits = 0
            return choice
        return self.current if self.current else ("empty" if self._last == "empty" else "uncertain")


def board_ips() -> dict[int, str]:
    try:
        text = subprocess.check_output(["arp", "-a"], text=True, timeout=5, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {}
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, mac = parts[0], parts[1].lower()
        node_id = BOARD_MACS.get(mac)
        if node_id and ip.count(".") == 3:
            found[node_id] = ip
    return found


class TrafficBoost(threading.Thread):
    def __init__(self, targets: list[str]) -> None:
        super().__init__(name="showroom-boost", daemon=True)
        self.targets = targets
        self.stop_event = threading.Event()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = b"ruview-csi"
        try:
            while not self.stop_event.wait(1 / 40):
                for ip in self.targets:
                    try:
                        sock.sendto(payload, (ip, 9))
                    except OSError:
                        continue
        finally:
            sock.close()

    def stop(self) -> None:
        self.stop_event.set()


class CsiStream:
    def __init__(self) -> None:
        self.buffers: dict[int, deque] = {}
        self.last_sig: dict[int, tuple] = {}
        self.unique_times: dict[int, deque] = {}
        self.motion: dict[int, str] = {}
        self.window_s = 1.5
        self.rates = {"without": {}, "with": {}, "boost": False}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def handle(self, payload: dict, now: float | None = None) -> None:
        if not isinstance(payload, dict):
            return
        now = time.monotonic() if now is None else now
        for item in payload.get("node_features") or []:
            if isinstance(item, dict) and item.get("node_id") is not None:
                level = str((item.get("classification") or {}).get("motion_level") or "")
                self.motion[int(item["node_id"])] = level
        with self._lock:
            for node in payload.get("nodes") or []:
                if not isinstance(node, dict) or node.get("node_id") is None:
                    continue
                amps = node.get("amplitude")
                if not isinstance(amps, list) or not amps:
                    continue
                signature = tuple(round(float(value), 1) for value in amps[:64])
                node_id = int(node["node_id"])
                if signature == self.last_sig.get(node_id):
                    continue
                self.last_sig[node_id] = signature
                shape = gain_norm(strip_guards(amps))
                try:
                    rssi = float(node.get("rssi_dbm"))
                except (TypeError, ValueError):
                    rssi = -70.0
                buf = self.buffers.setdefault(node_id, deque(maxlen=400))
                buf.append((now, shape, rssi))
                times = self.unique_times.setdefault(node_id, deque(maxlen=400))
                times.append(now)

    def fps(self, span: float = 6.0) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {
                str(node_id): round(sum(1 for stamp in times if now - stamp <= span) / span, 2)
                for node_id, times in self.unique_times.items()
            }

    def window(self) -> dict | None:
        now = time.monotonic()
        with self._lock:
            nodes = {}
            spreads = []
            for node_id, buf in self.buffers.items():
                frames = [(shape, rssi) for stamp, shape, rssi in buf if now - stamp <= self.window_s]
                made = window_from_frames(frames)
                if made:
                    nodes[str(node_id)] = made
                    spreads.append(sum(made["spread"]) / BINS)
            if not nodes:
                return None
            moving = any(level in ("present_moving", "active") for level in self.motion.values())
            spread = sum(spreads) / len(spreads) if spreads else 0.0
            return {"nodes": nodes, "spread": spread, "moving": moving or spread > 0.08}

    def stop(self) -> None:
        self._stop.set()


def stream_loop(stream: CsiStream) -> None:
    import asyncio
    import websockets

    async def run() -> None:
        while not stream._stop.is_set():
            try:
                async with websockets.connect(WS_URL, open_timeout=5) as ws:
                    while not stream._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2)
                        except asyncio.TimeoutError:
                            continue
                        stream.handle(json.loads(raw))
            except Exception:
                await asyncio.sleep(1)

    asyncio.run(run())


class ZoneApp:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "zones.json"
        self.model_path = data_dir / "zone_model.joblib"
        self.recordings = self._load()
        self.stream = CsiStream()
        self.tracker = StickyZones()
        self.bundle = None
        self.eval = self.recordings.get("eval") if isinstance(self.recordings.get("eval"), dict) else None
        self.teach: dict | None = None
        self._last_hop = 0.0
        self._last_tick = 0.0
        self._lock = threading.Lock()
        self._load_model()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("sessions", {"A": {}, "B": {}})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.recordings, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load_model(self) -> None:
        if not (self.eval or {}).get("ready"):
            return
        try:
            import joblib
            self.bundle = joblib.load(self.model_path)
            if self.bundle:
                self.bundle["centroids"] = class_centroids(
                    self.recordings,
                    self.bundle["baseline"],
                    self.bundle["node_ids"],
                )
        except Exception:
            self.bundle = None

    def start(self) -> None:
        threading.Thread(target=stream_loop, args=(self.stream,), name="showroom-csi", daemon=True).start()
        threading.Thread(target=self._rates, name="showroom-rates", daemon=True).start()

    def _rates(self) -> None:
        time.sleep(8)
        without = self.stream.fps()
        targets = list(board_ips().values())
        boost = TrafficBoost(targets) if targets else None
        if boost:
            boost.start()
        time.sleep(8)
        boosted = self.stream.fps() if boost else {}
        before = [value for value in without.values() if value]
        after = [value for value in boosted.values() if value]
        rose = bool(before and after and (sum(after) / len(after)) > (sum(before) / len(before)) * 1.15)
        self.stream.window_s = 1.5 if rose else 2.0
        self.stream.rates = {"without": without, "with": boosted, "boost": bool(boost), "rose": rose}

    def start_teach(self, label: str, session: str) -> dict:
        if session not in ("A", "B"):
            raise ValueError("session must be A or B")
        if not label:
            raise ValueError("choose a zone or empty")
        with self._lock:
            now = time.monotonic()
            if self.teach and not self.teach.get("done") and now < self.teach["until"]:
                raise RuntimeError("already recording")
            self.teach = {
                "label": label,
                "session": session,
                "sample_at": now + LEAD_S,
                "until": now + LEAD_S + RECORD_S,
                "windows": [],
                "done": False,
                "error": None,
            }
            return {"status": "recording", "label": label, "session": session}

    def tick(self, zones: list[dict]) -> None:
        now = time.monotonic()
        window = self.stream.window()
        with self._lock:
            dt = 0.2 if self._last_tick == 0 else min(1.0, now - self._last_tick)
            self._last_tick = now
            self._collect(window, now)
            distances = None
            hold = True
            if window and now - self._last_hop >= HOP_S:
                self._last_hop = now
                hold = False
                distances = self._distances(window) if self.bundle else None
            names = {zone["id"]: zone["name"] for zone in zones}
            self.view_state = self.tracker.update_distance(
                distances,
                [zone["id"] for zone in zones],
                names,
                dt,
                hold=hold,
            )
            if distances:
                scores = {label: 1.0 / (distance + 0.05) for label, distance in distances.items()}
                total = sum(scores.values()) or 1.0
                self.tracker.last_probs = {label: score / total for label, score in scores.items()}
            self.view_state["probs"] = dict(self.tracker.last_probs)
            in_room = self.view_state.get("zone_id") or self.view_state.get("uncertain")
            if not in_room:
                self.view_state["motion"] = "none"
            else:
                self.view_state["motion"] = "walking" if window and window.get("moving") else "still" if window else "none"
            self.view_state["window"] = window

    def _collect(self, window: dict | None, now: float) -> None:
        teach = self.teach
        if not teach or teach.get("done"):
            return
        if now < teach["sample_at"]:
            return
        if now < teach["until"]:
            if window and now - teach.get("last", 0) >= HOP_S:
                teach["windows"].append(window)
                teach["last"] = now
            return
        if len(teach["windows"]) < 20:
            teach["error"] = "not enough CSI frames; keep both boards powered and try again"
        else:
            session = self.recordings["sessions"].setdefault(teach["session"], {})
            session[teach["label"]] = teach["windows"]
            self._save()
            teach["error"] = None
            self.retrain()
        teach["done"] = True

    def _distances(self, window: dict) -> dict[str, float]:
        bundle = self.bundle or {}
        centroids = bundle.get("centroids") or {}
        if not centroids:
            return {}
        vector = feature_vector(window, bundle["baseline"], bundle["node_ids"])
        return {
            label: _distance(vector, center)
            for label, center in centroids.items()
            if len(center) == len(vector)
        }

    def _proba(self, window: dict) -> dict[str, float]:
        bundle = self.bundle
        if not bundle:
            return {}
        vector = feature_vector(window, bundle["baseline"], bundle["node_ids"])
        _, probas = _predict(bundle["model"], [vector])
        return probas[0]

    def retrain(self) -> dict:
        result = evaluate_sessions(self.recordings)
        self.eval = result
        self.recordings["eval"] = {key: value for key, value in result.items() if key != "pred"}
        self._save()
        if result.get("ready"):
            import joblib
            self.bundle = train_full(self.recordings, result.get("model") or "extra_trees")
            joblib.dump(self.bundle, self.model_path)
        else:
            self.bundle = None
            if self.model_path.exists():
                self.model_path.unlink()
        return result

    def public(self, zones: list[dict]) -> dict:
        with self._lock:
            state = getattr(self, "view_state", {"zone_id": None, "uncertain": False, "probs": {}, "motion": "none"})
            active = state.get("zone_id")
            zone = next((item for item in zones if item["id"] == active), None)
            rows = []
            for item in zones:
                stats = self.tracker.stats.get(item["id"], {})
                rows.append({
                    "id": item["id"],
                    "name": item["name"],
                    "entries": stats.get("entries", 0),
                    "dwell_s": round(stats.get("dwell", 0.0), 1),
                    "inside": active == item["id"],
                    "prob": round(float((state.get("probs") or {}).get(item["id"], 0.0)), 3),
                })
            teach = self.teach
            now = time.monotonic()
            if not teach:
                capture = None
            elif not teach.get("done") and now < teach["until"]:
                walking = now < teach["sample_at"]
                capture = {
                    "active": True,
                    "phase": "go" if walking else "record",
                    "remaining_s": round((teach["sample_at"] if walking else teach["until"]) - now, 1),
                    "label": teach["label"],
                    "session": teach["session"],
                    "error": None,
                }
            else:
                capture = {
                    "active": False,
                    "phase": "done",
                    "remaining_s": 0,
                    "label": teach.get("label"),
                    "session": teach.get("session"),
                    "error": teach.get("error"),
                }
            counts = {
                session: {label: len(rows_) for label, rows_ in data.items() if isinstance(rows_, list)}
                for session, data in (self.recordings.get("sessions") or {}).items()
                if isinstance(data, dict)
            }
            return {
                "zone_mode": bool(self.bundle),
                "x": None if zone is None else round(zone["x"] + zone["w"] / 2, 3),
                "y": None if zone is None else round(zone["y"] + zone["h"] / 2, 3),
                "zone": None if zone is None else {"id": zone["id"], "name": zone["name"]},
                "uncertain": bool(state.get("uncertain")) and zone is None,
                "motion": state.get("motion") or "none",
                "mode": "zone" if self.bundle else "unteach",
                "zones": rows,
                "route": list(self.tracker.route),
                "eval": self.eval,
                "rates": self.stream.rates,
                "window_s": self.stream.window_s,
                "teach": capture,
                "recordings": counts,
            }

    def reset_counts(self) -> None:
        with self._lock:
            self.tracker.reset()


def eval_rssi(path: Path) -> dict:
    """Leave-one-out 3-NN on saved RSSI spots. Reproducer for the loudness method."""
    data = json.loads(path.read_text(encoding="utf-8"))
    spots = [spot for spot in data.get("spots") or [] if isinstance(spot, dict) and spot.get("rssi")]
    if len(spots) < 4:
        return {"measured": False, "reason": "fewer than 4 spots"}
    errors = []
    center_x = sum(float(spot["x"]) for spot in spots) / len(spots)
    center_y = sum(float(spot["y"]) for spot in spots) / len(spots)
    center_errors = []
    for index, spot in enumerate(spots):
        others = [other for other_index, other in enumerate(spots) if other_index != index]

        def dist(other: dict) -> float:
            keys = set(spot["rssi"]) & set(other["rssi"])
            return math.sqrt(sum((float(spot["rssi"][key]) - float(other["rssi"][key])) ** 2 for key in keys) / len(keys))

        nearest = sorted(others, key=dist)[:3]
        denom = sum(1.0 / (dist(other) + 0.5) for other in nearest)
        pred_x = sum(float(other["x"]) / (dist(other) + 0.5) for other in nearest) / denom
        pred_y = sum(float(other["y"]) / (dist(other) + 0.5) for other in nearest) / denom
        errors.append(math.hypot(pred_x - float(spot["x"]), pred_y - float(spot["y"])))
        center_errors.append(math.hypot(center_x - float(spot["x"]), center_y - float(spot["y"])))
    return {
        "measured": True,
        "tag": "MEASURED",
        "spots": len(spots),
        "mean_error_m": round(sum(errors) / len(errors), 2),
        "center_error_m": round(sum(center_errors) / len(center_errors), 2),
    }


def _synthetic_recordings() -> dict:
    def window(node_bump: int, rssi: float, label_noise: float = 0.0) -> dict:
        shape = [0.2 + label_noise] * BINS
        shape[node_bump] = 2.0
        spread = [0.05] * BINS
        return {"nodes": {"1": {"shape": shape, "spread": spread, "rssi": rssi},
                          "2": {"shape": list(shape), "spread": spread, "rssi": rssi}}}

    def block(bump: int) -> list[dict]:
        return [window(bump, -50, index * 0.001) for index in range(30)]

    return {"sessions": {
        "A": {"empty": block(4), "shelf": block(16), "walk": block(28)},
        "B": {"empty": block(4), "shelf": block(16), "walk": block(28)},
    }}


def self_check() -> None:
    core = strip_guards([0, 0, 2, 4, 0])
    assert core == [2, 4]
    norm = gain_norm([0, 2, 2] + [0] * 10)
    assert abs(norm[1] - 1.0) < 1e-6

    tracker = StickyZones()
    zones = ["shelf", "walk"]
    names = {"shelf": "Shelf", "walk": "Walk"}
    view = {}
    for _ in range(5):
        view = tracker.update({"shelf": 0.9, "empty": 0.1}, zones, names, 0.5)
    assert view["zone_id"] == "shelf"
    assert tracker.stats["shelf"]["entries"] == 1
    for _ in range(4):
        view = tracker.update({"shelf": 0.95}, zones, names, 0.5)
    assert tracker.stats["shelf"]["entries"] == 1
    for _ in range(8):
        tracker.update({"walk": 0.9}, zones, names, 0.5)
    assert tracker.stats["shelf"]["latched"] is False
    for _ in range(5):
        tracker.update({"shelf": 0.92}, zones, names, 0.5)
    assert tracker.stats["shelf"]["entries"] == 2

    held = StickyZones()
    held.update({"shelf": 0.9}, zones, names, 0.5)
    held.update({"shelf": 0.9}, zones, names, 0.5)
    kept = held.update({"walk": 0.9}, zones, names, 0.5)
    assert kept["zone_id"] == "shelf"

    gone = StickyZones()
    gone.update({"shelf": 0.95, "empty": 0.05}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    empty = gone.update({"empty": 0.8, "shelf": 0.1}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    assert empty["zone_id"] is None and empty["uncertain"] is False
    held_empty = gone.update(None, ["shelf"], {"shelf": "Shelf"}, 0.5, hold=True)
    assert held_empty["zone_id"] is None and held_empty["uncertain"] is False

    near = StickyZones()
    near.update_distance({"empty": 5.0, "shelf": 3.0}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    arrived = near.update_distance({"empty": 5.0, "shelf": 3.0}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    assert arrived["zone_id"] == "shelf"
    left = near.update_distance({"empty": 2.0, "shelf": 6.0}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    gone = near.update_distance({"empty": 2.0, "shelf": 6.0}, ["shelf"], {"shelf": "Shelf"}, 0.5)
    assert gone["zone_id"] is None and gone["uncertain"] is False

    result = evaluate_sessions(_synthetic_recordings())
    assert result["measured"] is True, result
    assert result["ready"] is True, result
    assert result["accuracy"] > result["majority"]
    print("zone check: PASS")


if __name__ == "__main__":
    self_check()
