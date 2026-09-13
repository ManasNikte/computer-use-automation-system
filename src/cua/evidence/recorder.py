"""
Run evidence: structured event log + richer artefacts on failure.

Every run -- discovery, replay, or operator handoff -- gets a directory under
`evidence/<run_id>/` containing:

    events.jsonl        one JSON object per event, append-only
    <label>.png         screenshots, captured on failure and at handoff
    <label>.controls.json  flattened control tree, captured alongside

Design notes:

* **Redaction is applied in the sink**, not by callers. `log()` runs every
  payload through the redactor before it is serialized. A future caller who
  logs raw page text cannot leak a tax ID by forgetting to redact, because
  there is no path to the file that skips it.

* **JSON Lines, append-only.** It survives a crashed run (you keep everything
  written before the crash), it streams, and it needs no schema migration.

* **Events carry `why`, not just `what`.** The brief asks for a log of what the
  agent did *and why*; for discovery that is the model's stated reasoning, for
  replay it is the artifact step id that mandated the action. Both answer
  "why did this click happen" without a transcript.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from ..safety.redaction import Redactor


def new_run_id(prefix: str) -> str:
    return "{}_{}_{}".format(prefix, time.strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:6])


class EvidenceRecorder:
    def __init__(self, root: str, run_id: str, redactor: Optional[Redactor] = None,
                 echo: bool = False):
        self.run_id = run_id
        self.dir = os.path.join(root, run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "events.jsonl")
        self.redactor = redactor or Redactor()
        self.echo = echo
        self._seq = 0
        self._events: List[Dict[str, Any]] = []

    # -- events ------------------------------------------------------------
    def log(self, event: str, **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": round(time.time(), 3),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(self.redactor.redact_obj(fields))
        self._events.append(record)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        if self.echo:
            print("  · {:<22} {}".format(
                event, " ".join("{}={}".format(k, v) for k, v in fields.items()
                                if k not in ("page_text",))[:160]))
        return record

    def events(self) -> List[Dict[str, Any]]:
        return list(self._events)

    # -- richer signals ----------------------------------------------------
    def capture(self, surface, label: str) -> Dict[str, Optional[str]]:
        """Screenshot + control-tree snapshot.

        Called on every hard failure and at every control handoff. The pair
        matters: the screenshot tells a human what the screen looked like, the
        control tree tells an engineer why a locator missed.
        """
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        shot = os.path.join(self.dir, "{}.png".format(safe_label))
        tree = os.path.join(self.dir, "{}.controls.json".format(safe_label))
        shot_path = surface.screenshot(shot)
        tree_path = surface.structure_snapshot(tree)
        if tree_path:
            self._redact_file_in_place(tree_path)
        self.log("evidence_captured", label=label,
                 screenshot=os.path.basename(shot_path) if shot_path else None,
                 controls=os.path.basename(tree_path) if tree_path else None)
        return {"screenshot": shot_path, "controls": tree_path}

    def write_json(self, name: str, payload: Any) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.redactor.redact_obj(payload), fh, indent=2, sort_keys=True)
        return path

    def _redact_file_in_place(self, path: str) -> None:
        """The control tree can carry field *values* scraped off the screen."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.redactor.redact_obj(data), fh, indent=2)
        except (OSError, ValueError):
            pass
