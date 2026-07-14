"""Configure JSON-formatted stdout logging for the admin app."""

import json
import logging
import sys


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in (
            "action_id",
            "step",
            "target",
            "duration_ms",
            "outcome",
            "incident_id",
            "component",
        ):
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload)


def configure() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
