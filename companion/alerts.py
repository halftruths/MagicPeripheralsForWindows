"""Low-battery alerts. Polls the device monitor and publishes one "Low battery" action event per crossing.

A device alerts when it is connected, not charging, alerts are enabled for it and percent <= threshold. It then stays
quiet until it is re-armed: the level rises above threshold + REARM_MARGIN, or the device charges / disconnects.
"""

import threading
import time
import traceback

REARM_MARGIN = 5
POLL_SECONDS = 60

# status "device" name -> settings key prefix
PREFIXES = {"Magic Trackpad": "tp", "Magic Keyboard": "kb"}


class BatteryAlerts:
    def __init__(self, get_status, settings, publish, name="Low battery"):
        """get_status() -> list of status dicts; settings.get(key); publish(event dict)."""
        self.get_status, self.settings, self.publish, self.name = get_status, settings, publish, name
        self.alerted = set()
        self.stop_event = threading.Event()

    def check(self, statuses=None):
        """One evaluation pass. Returns the events published."""
        if statuses is None:
            statuses = self.get_status()
        events = []
        for item in statuses:
            device = item.get("device")
            prefix = PREFIXES.get(device)
            if not prefix:
                continue
            percent = item.get("percent")
            if not item.get("connected") or item.get("charging") or percent is None:
                self.alerted.discard(device)  # charging / away: next time it drains it should alert again
                continue
            enabled = self.settings.get(prefix + ".batteryAlert")
            threshold = self.settings.get(prefix + ".batteryThreshold")
            if percent > threshold + REARM_MARGIN:
                self.alerted.discard(device)
            if not enabled or device in self.alerted or percent > threshold:
                continue
            self.alerted.add(device)
            event = {"type": "action", "t": time.perf_counter(), "name": self.name, "detail": f"{device} {percent}%"}
            events.append(event)
            self.publish(event)
        return events

    def run(self):
        while True:
            try:
                self.check()
            except Exception:
                traceback.print_exc()  # never let alerting kill the thread
            if self.stop_event.wait(POLL_SECONDS):
                return

    def start(self):
        threading.Thread(target=self.run, name="battery-alerts", daemon=True).start()
        return self

    def stop(self):
        self.stop_event.set()
