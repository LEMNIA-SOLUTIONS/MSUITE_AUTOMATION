"""Walk one automation run through the canvas graph.

A run is a set of *paths*. A path sits on a node; executing the node returns
the output handle to follow. Several wires on that handle = fan-out: the path
takes the first target (by position) and new paths take the others. A node
with several incoming wires is a *join*: a path arriving there parks as
"At join" until no other open path of the run can still reach the node, then
exactly one path continues (wait-for-all). Pure: the executor and the dry run
pass their own `execute` callback."""

from __future__ import annotations

from msuite_automation.msuite_automation.constants import (
	PATH_AT_JOIN, PATH_ENDED, PATH_RUNNING, PATH_WAITING, WAIT_SIGNAL, WALK_STEP_LIMIT,
)


def start_paths(trigger_id: str) -> list[dict]:
	return [{"path_id": "p1", "node_id": trigger_id, "status": PATH_RUNNING, "executed": True, "joined": False, "handle": ""}]


def _can_reach(graph, path, target) -> bool:
	node = path["node_id"]
	if path["status"] == PATH_RUNNING and not path.get("executed"):
		return node == target or target in graph.reachable(node)
	return target in graph.reachable(node)


def _release_joins(graph, paths) -> bool:
	released = False
	for target in dict.fromkeys(p["node_id"] for p in paths if p["status"] == PATH_AT_JOIN):
		waiting = [p for p in paths if p["status"] == PATH_AT_JOIN and p["node_id"] == target]
		others = [p for p in paths if p["status"] in (PATH_RUNNING, PATH_WAITING, PATH_AT_JOIN) and p not in waiting]
		if any(_can_reach(graph, p, target) for p in others):
			continue
		keep, *merged = waiting
		keep.update(status=PATH_RUNNING, executed=False, joined=True)
		for p in merged:
			p["status"] = PATH_ENDED
		released = True
	return released


def advance(graph, paths: list[dict], execute, new_id, limit: int = WALK_STEP_LIMIT) -> list[dict]:
	steps = 0
	while True:
		path = next((p for p in paths if p["status"] == PATH_RUNNING), None)
		if path is None:
			if _release_joins(graph, paths):
				continue
			return paths
		steps += 1
		if steps > limit:
			raise RuntimeError(f"Automation run stopped after {limit} steps")
		if not path.get("executed"):
			if graph.is_join(path["node_id"]) and not path.get("joined"):
				path["status"] = PATH_AT_JOIN
				continue
			handle = execute(path, path["node_id"])
			path["executed"] = True
			if handle == WAIT_SIGNAL:
				path["status"] = PATH_WAITING
				continue
		else:
			handle = path.get("handle") or ""
		visited = path.get("visited") or [path["node_id"]]
		targets = []
		for t in graph.targets(path["node_id"], handle):
			if t in visited:  # guard only: validation already rejects cycles
				path["error"] = f"Loop: {t} already ran on this path"
			else:
				targets.append(t)
		if not targets:
			path["status"] = PATH_ENDED
			continue
		path.update(node_id=targets[0], executed=False, joined=False, handle="", visited=visited + [targets[0]])
		for t in targets[1:]:
			paths.append({"path_id": new_id(), "node_id": t, "status": PATH_RUNNING, "executed": False, "joined": False,
				"handle": "", "visited": visited + [t]})
