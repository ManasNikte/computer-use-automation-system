"""
Capability store and the agent-facing catalog.

Storage is a directory of JSON files named `<id>.v<version>.json`. That is a
deliberate choice, not laziness: capability artifacts are small, they need to
be **diffable and reviewable in a pull request**, and the approval step is a
human reading one. A database would make the review story worse and buy
nothing at this size. Swapping in a real registry later is a change to this
file only.

Versioning is explicit and immutable: re-recording a capability writes v2 and
leaves v1 alone, because something in production may be pinned to v1 and
because a reviewer needs to diff the two.

The catalog is the stretch-goal piece -- it presents the stored artifacts to a
calling AI agent as a list of typed, named tools. That is the whole point of
the artifact being a contract rather than a step list: the caller picks a
capability by name, passes typed args, and gets typed results, without ever
knowing there is a browser involved.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .schema import Capability

_NAME_RE = re.compile(r"^(?P<id>[a-zA-Z0-9_\-]+)\.v(?P<version>\d+)\.json$")


class CapabilityStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    # -- paths -------------------------------------------------------------
    def path_for(self, capability_id: str, version: int) -> str:
        return os.path.join(self.root, "{}.v{}.json".format(capability_id, version))

    # -- read --------------------------------------------------------------
    def list(self) -> List[Capability]:
        out: List[Capability] = []
        for fname in sorted(os.listdir(self.root)):
            if not _NAME_RE.match(fname):
                continue
            try:
                out.append(self.load_path(os.path.join(self.root, fname)))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def load_path(self, path: str) -> Capability:
        with open(path, "r", encoding="utf-8") as fh:
            return Capability.from_dict(json.load(fh))

    def load(self, capability_id: str, version: Optional[int] = None) -> Capability:
        if version is not None:
            path = self.path_for(capability_id, version)
            if not os.path.exists(path):
                raise FileNotFoundError(
                    "No capability {!r} version {} in {}".format(capability_id, version, self.root))
            return self.load_path(path)
        versions = self.versions(capability_id)
        if not versions:
            raise FileNotFoundError(
                "No capability {!r} in {}".format(capability_id, self.root))
        return self.load(capability_id, max(versions))

    def versions(self, capability_id: str) -> List[int]:
        out: List[int] = []
        for fname in os.listdir(self.root):
            match = _NAME_RE.match(fname)
            if match and match.group("id") == capability_id:
                out.append(int(match.group("version")))
        return sorted(out)

    # -- write -------------------------------------------------------------
    def save(self, capability: Capability, *, overwrite: bool = False) -> str:
        """Persist a capability.

        Refuses to clobber an existing version unless told to: silently
        replacing v1 while something is invoking it is how you get an outage
        that no diff explains.
        """
        path = self.path_for(capability.id, capability.version)
        if os.path.exists(path) and not overwrite:
            raise FileExistsError(
                "{} already exists. Bump the version or pass overwrite=True.".format(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(capability.to_dict(), fh, indent=2, sort_keys=False)
            fh.write("\n")
        return path

    def next_version(self, capability_id: str) -> int:
        versions = self.versions(capability_id)
        return (max(versions) + 1) if versions else 1

    def set_status(self, capability_id: str, version: int, status: str) -> Capability:
        """The draft -> approved gate.

        This is a safety control, not bookkeeping: an unapproved capability
        cannot execute an irreversible step unattended (see safety/policy.py).
        """
        if status not in ("draft", "approved"):
            raise ValueError("status must be 'draft' or 'approved'")
        capability = self.load(capability_id, version)
        capability.status = status
        self.save(capability, overwrite=True)
        return capability


class CapabilityCatalog:
    """The agent-facing view of the store.

    A calling agent asks for `tools()` and gets JSON-Schema tool definitions --
    the same shape it would get from any function-calling interface. It never
    sees steps, locators, or the fact that a browser exists.

    `approved_only` defaults to True because the catalog is the *production*
    surface. A draft capability is by definition one no human has signed off,
    and the whole point of the approval gate is that it is not invocable
    unattended.
    """

    def __init__(self, store: CapabilityStore, approved_only: bool = True):
        self.store = store
        self.approved_only = approved_only

    def capabilities(self) -> List[Capability]:
        latest: Dict[str, Capability] = {}
        for capability in self.store.list():
            if self.approved_only and capability.status != "approved":
                continue
            current = latest.get(capability.id)
            if current is None or capability.version > current.version:
                latest[capability.id] = capability
        return [latest[k] for k in sorted(latest)]

    def tools(self) -> List[Dict[str, Any]]:
        return [c.to_tool_schema() for c in self.capabilities()]

    def get(self, capability_id: str) -> Capability:
        for capability in self.capabilities():
            if capability.id == capability_id:
                return capability
        raise KeyError(
            "No {}capability named {!r} in the catalog.".format(
                "approved " if self.approved_only else "", capability_id))
