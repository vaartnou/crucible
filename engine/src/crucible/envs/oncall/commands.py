"""Strict supported command grammar and effects."""

import shlex
from dataclasses import asdict

from pydantic import JsonValue

from crucible.envs.oncall.model import State
from crucible.envs.oncall.telemetry import pod_table
from crucible.envs.oncall.world import World


def run(world: World, state: State, command: str) -> tuple[JsonValue, int]:
    words = shlex.split(command)
    names = {s.name for s in world.services}
    if words == ["kubectl", "get", "services"]:
        return [
            {"name": s.name, "kind": s.kind, "dependencies": list(s.dependencies)}
            for s in world.services
        ], 1
    if words in (["kubectl", "get", "pods"], ["kubectl", "get", "pods", "-n", "shop"]):
        return pod_table(state.history[-1]), 1
    if words == ["kubectl", "get", "events"]:
        return [
            {"pod": p.pod, "reason": p.state, "minute": p.minute}
            for p in state.history[-1]
            if p.state != "Running"
        ], 1
    if words[:2] == ["edges", "list"] and len(words) in (2, 3):
        if len(words) == 3 and words[2] not in names:
            raise ValueError("unknown service")
        return [
            asdict(edge)
            for edge in state.edge_history[-1]
            if len(words) == 2 or edge.caller == words[2]
        ], 1
    if words == ["changes", "list"]:
        return [
            {
                "id": c.id,
                "minute": c.minute,
                "service": c.service,
                "kind": c.kind,
                "author": c.author,
            }
            for c in world.changes
            if c.minute <= state.minute
        ], 1
    if len(words) == 3 and words[:2] == ["changes", "list"]:
        if words[2] not in names:
            raise ValueError("unknown service")
        return [
            asdict(c) for c in world.changes if c.service == words[2] and c.minute <= state.minute
        ], 1
    if len(words) == 3 and words[:2] == ["config", "history"] and words[2] in names:
        return [
            asdict(c)
            for c in world.changes
            if c.service == words[2] and c.kind == "config" and c.minute <= state.minute
        ], 1
    if len(words) == 4 and words[:3] == ["kubectl", "rollout", "history"]:
        name = words[3].removeprefix("deploy/")
        if name in names:
            return [
                asdict(c)
                for c in world.changes
                if c.service == name and c.kind == "deploy" and c.minute <= state.minute
            ], 1
    operation, target, revision = "", "", ""
    if len(words) in (4, 5) and words[:3] == ["kubectl", "rollout", "undo"]:
        operation, target = "undo", words[3].removeprefix("deploy/")
        candidates = [
            c
            for c in world.changes
            if c.service == target
            and c.kind == "deploy"
            and c.minute <= state.minute
            and c.id not in state.reverted_changes
        ]
        if len(words) == 5:
            if not words[4].startswith("--to-revision="):
                raise ValueError("expected --to-revision=CHANGE_ID")
            revision = words[4].split("=", 1)[1]
            if not any(c.id == revision for c in candidates):
                raise ValueError("unknown or already reverted deployment revision")
        elif candidates:
            revision = max(candidates, key=lambda c: c.minute).id
    elif len(words) == 4 and words[:3] == ["kubectl", "rollout", "restart"]:
        operation, target = "restart", words[3].removeprefix("deploy/")
    elif len(words) == 4 and words[:2] == ["config", "revert"]:
        operation, target, revision = "config", words[2], words[3]
        if not any(
            c.id == revision
            and c.service == target
            and c.kind == "config"
            and c.minute <= state.minute
            for c in world.changes
        ):
            raise ValueError("unknown configuration revision")
    if target not in names:
        raise ValueError(
            "unknown command or service; see the supported command grammar in the instruction"
        )
    state.pending_action = (operation, target, revision)
    return {"message": f"{operation} accepted for {target}", "revision": revision}, 3
