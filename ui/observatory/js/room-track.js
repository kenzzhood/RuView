/**
 * Turn two live ESP32 nodes into one smoothed place in the room.
 * Sensor 1 is the left side (first board). Sensor 2 is the right side
 * (second board). The marker slides toward whichever board hears a
 * stronger signal. Two boards cannot give a calibrated floor plan.
 */
export class RoomTracker {
  constructor() {
    this._t = 0.5;
    this._rssi = null;
    this._walk = 0;
    this._walking = false;
    this._presentHold = 0;
  }

  apply(frame) {
    const raw = Array.isArray(frame?.node_features) ? frame.node_features : [];
    const nodes = raw
      .filter((n) => n && n.node_id != null && n.stale !== true && (n.last_seen_ms == null || n.last_seen_ms < 4000))
      .sort((a, b) => a.node_id - b.node_id);
    if (nodes.length === 0) return null;

    const side = nodes.map((n) => {
      const level = n.classification?.motion_level || n.node_inference?.classification || 'absent';
      const present = (!!n.classification?.presence && level !== 'absent') || level === 'present_still' || level === 'present_moving' || level === 'active';
      const motion = Number(n.features?.motion_band_power) || 0;
      const rssi = Number(n.rssi_dbm ?? n.features?.mean_rssi);
      return {
        id: n.node_id,
        present,
        level,
        motion,
        rssi: Number.isFinite(rssi) ? rssi : -70,
      };
    });

    const anyPresent = side.some((s) => s.present);
    if (anyPresent) this._presentHold = 1;
    else this._presentHold = Math.max(0, this._presentHold - 0.03);
    const present = anyPresent || this._presentHold > 0.2;

    let target = 0.5;
    if (side.length >= 2) {
      const left = side[0];
      const right = side[1];
      const db = right.rssi - left.rssi;
      target = 0.5 + Math.max(-0.42, Math.min(0.42, db / 16));
    } else if (present) {
      target = side[0].id === 1 ? 0.1 : 0.9;
    }
    target = Math.min(0.92, Math.max(0.08, target));

    const moving = side.some((s) => s.present && (s.level === 'present_moving' || s.level === 'active'));
    if (!present) this._walk += (0 - this._walk) * 0.08;
    else if (moving) this._walk += (1 - this._walk) * 0.08;
    else this._walk += (0 - this._walk) * 0.05;
    this._walking = this._walking ? this._walk > 0.32 : this._walk > 0.62;

    this._t += (target - this._t) * (this._walking ? 0.16 : 0.07);

    const rssiNow = side.reduce((sum, s) => sum + s.rssi, 0) / side.length;
    this._rssi = this._rssi == null ? rssiNow : this._rssi * 0.9 + rssiNow * 0.1;

    const near = this._t < 0.38 ? 'sensor 1' : this._t > 0.62 ? 'sensor 2' : 'the middle';
    const pose = this._walking ? 'walking' : 'standing';
    const label = !present
      ? 'Room looks empty'
      : this._walking
        ? `Walking, closer to ${near}`
        : `Standing, closer to ${near}`;

    const x = -2.6 + this._t * 5.2;
    return {
      persons: present
        ? [{ id: 1, position: [x, 0, 0.2], pose, motion_score: this._walking ? 0.85 : 0.04, facing: 0 }]
        : [],
      estimated_persons: present ? 1 : 0,
      label,
      placeText: !present ? 'Empty' : this._t < 0.38 ? 'Sensor 1' : this._t > 0.62 ? 'Sensor 2' : 'Middle',
      motionText: !present ? 'None' : this._walking ? 'Walking' : 'Standing',
      rssi: this._rssi,
      t: this._t,
      present,
      walking: present && this._walking,
      sensors: side.map((s) => ({ id: s.id, present: s.present })),
    };
  }
}
