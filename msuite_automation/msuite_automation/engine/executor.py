"""Run an automation for one document on the canvas graph: checks →
Messaging Automation Run row → walk paths (fan-out, joins, Wait parks a path) → log.
resume_waiting_runs() continues parked paths from the scheduler."""

from __future__ import annotations

import itertools
import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime, nowdate
from frappe.utils.safe_exec import get_safe_globals, safe_exec

from msuite_core.constants import LOGGER_NAME
from msuite_automation.msuite_automation.constants import (
	ADD_TO_LIST, DOCTYPE, IF_ELSE, ONCE_PER_DAY, ONCE_PER_DOCUMENT, OUTBOX_DOCTYPE, PATH_RUNNING, PATH_WAITING, QUEUE_SHORT,
	RETRY_NOTE_MAX, RUN_CLAIM_TTL_SECONDS, RUN_DOCTYPE, RUN_ERROR_MAX, SCHEDULE, SEND_EMAIL, SEND_SMS, SEND_WHATSAPP,
	STUCK_SWEEP_BATCH, SWITCH, WAIT, WAIT_EVENT, WAIT_SIGNAL,
)
from msuite_automation.msuite_automation.engine import channels
from msuite_automation.msuite_automation.engine import steps as S
from msuite_automation.msuite_automation.engine.event_check import event_met, wait_key
from msuite_automation.msuite_automation.engine.filters import field_value, normalize, rules_pass
from msuite_automation.msuite_automation.engine.graph import Graph, switch_cases
from msuite_automation.msuite_automation.engine.walker import advance, start_paths

WAIT_UNITS = {"Minutes": "minutes", "Hours": "hours", "Days": "days"}
RETRY_DELAYS = {"transient": (60, 300, 900), "limit": (1800,) * 48}
STUCK_AFTER_MINUTES = 30  # RQ short jobs time out in minutes; a longer Running row means the worker died
SCHEDULE_BATCH = 500  # names per page query
DUE_BATCH = 500
EVENT_CHECK_CAP = 200
MAX_FAILED_RUNS = 3  # Once per day / Once per document: failed runs of one document before schedule ticks and events stop re-running it
OUTBOX_REDELIVER_AFTER_MINUTES = 5  # an unclaimed outbox row older than this had its queued job lost
OUTBOX_REDELIVER_BATCH = 200  # rows re-queued per tick, so a long Redis outage does not flood the queue on recovery
OUTBOX_JOB_PREFIX = "outbox"
DELIVER_OUTBOX_PATH = "msuite_automation.msuite_automation.engine.executor.deliver_outbox"
RUN_BATCH_PATH = "msuite_automation.msuite_automation.engine.executor.run_batch"
JOB_BATCH = 25  # documents per background job (~25 s of sends, well under the RQ short timeout)
JOB_TIMEOUT = 1500  # seconds: below STUCK_AFTER_MINUTES (1800s), above JOB_BATCH (25) x 30s REQUEST_TIMEOUT
SCHEDULE_CEILING = 50_000  # ponytail: max pending names yielded per tick (documents that fail the trigger filters or condition never get a run row, so they count as pending every tick; Every time restarts from the beginning each tick)
SCHEDULE_SCAN_LIMIT = 2_000_000  # backstop across already-run rows; stop silently with one log if hit


def enqueue_safely(method, **kwargs) -> None:
	"""Queue `method` after the current transaction commits. A queue error is logged, never raised:
	an exception from an after-commit callback would escape db.commit() and skip every callback behind it.
	Side effects: adds one frappe.db.after_commit callback."""
	def enqueue_call():
		"""Attempt to enqueue method; log and swallow any exception."""
		try:
			frappe.enqueue(method, **kwargs)
		except Exception:
			frappe.log_error(title=f"Messaging Automation enqueue failed: {method}")

	frappe.db.after_commit.add(enqueue_call)


def enqueue_run(name, trigger_id, doctype, docname, value="", discriminator="", doc_dict=None, skip_checks=False, phone=None):
	"""Record a trigger event in the outbox (same transaction as the caller's save, so it commits or rolls
	back with it) and queue its delivery after commit. If the queued job is lost, redeliver_outbox re-queues it."""
	payload = dict(name=name, trigger_id=trigger_id, doctype=doctype, docname=docname, value=value,
		discriminator=discriminator, doc_dict=doc_dict, skip_checks=skip_checks, phone=phone)
	row = frappe.get_doc({"doctype": OUTBOX_DOCTYPE, "payload": json.dumps(payload, default=str)}).insert(ignore_permissions=True)
	enqueue_safely(DELIVER_OUTBOX_PATH, queue=QUEUE_SHORT,
		job_id=f"{OUTBOX_JOB_PREFIX}:{row.name}", deduplicate=True, outbox=row.name)
	return row.name


def deliver_outbox(outbox) -> None:
	"""Claim an outbox row by deleting it, then run its automation. The DELETE row count is the claim, so a
	duplicate delivery finds nothing and does nothing (never runs twice). Commits right after the claim."""
	payload = frappe.db.get_value(OUTBOX_DOCTYPE, outbox, "payload")
	if not payload:
		return
	frappe.db.sql(f"delete from `tab{OUTBOX_DOCTYPE}` where name=%s", outbox)
	if not frappe.db.sql("select row_count()")[0][0]:
		return
	frappe.db.commit()
	run_automation(**json.loads(payload))


def redeliver_outbox() -> None:
	"""Scheduled safety net: re-queue outbox rows nobody claimed within OUTBOX_REDELIVER_AFTER_MINUTES
	(Redis was down or flushed). Capped per tick; a failing enqueue is logged and the rest continue."""
	cutoff = add_to_date(now_datetime(), minutes=-OUTBOX_REDELIVER_AFTER_MINUTES)
	names = frappe.get_all(OUTBOX_DOCTYPE, filters={"creation": ("<", cutoff)}, order_by="creation asc",
		limit=OUTBOX_REDELIVER_BATCH + 1, pluck="name")
	if len(names) > OUTBOX_REDELIVER_BATCH:
		frappe.logger(LOGGER_NAME).warning(f"redeliver_outbox: more than {OUTBOX_REDELIVER_BATCH} undelivered events, the rest wait for the next tick")
		names = names[:OUTBOX_REDELIVER_BATCH]
	for name in names:
		try:
			frappe.enqueue(DELIVER_OUTBOX_PATH, queue=QUEUE_SHORT,
				job_id=f"{OUTBOX_JOB_PREFIX}:{name}", deduplicate=True, outbox=name)
		except Exception:
			frappe.log_error(title=f"Messaging Automation outbox redelivery failed: {name}")


def enqueue_batch(name, trigger_id, doctype, docnames):
	"""Queue a batch run after transaction commit safely."""
	enqueue_safely(RUN_BATCH_PATH, queue=QUEUE_SHORT,
		timeout=JOB_TIMEOUT, name=name, trigger_id=trigger_id, doctype=doctype, docnames=docnames)


def enqueue_pending(name, trigger_id, doctype, send_frequency, base_filters=None) -> None:
	for pending in iter_pending_names(name, send_frequency, doctype, base_filters=base_filters):
		for i in range(0, len(pending), JOB_BATCH):
			enqueue_batch(name, trigger_id, doctype, pending[i:i + JOB_BATCH])


def run_batch(name, trigger_id, doctype, docnames) -> None:
	for docname in (docnames or []):
		try:
			run_automation(name=name, trigger_id=trigger_id, doctype=doctype, docname=docname)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Messaging Automation {name} batch run failed: {docname}")
		finally:
			frappe.db.commit()


def _load_doc(doctype, docname, doc_dict):
	if doc_dict:
		return frappe.get_doc(doc_dict)
	if not frappe.db.exists(doctype, docname):
		return None
	return frappe.get_doc(doctype, docname)


def _already_ran(automation_name, send_frequency, doctype, docname) -> bool:
	return bool(docname) and str(docname) in _names_already_run(automation_name, send_frequency, doctype, [docname])


def _names_already_run(automation_name, send_frequency, doctype, names: list) -> set[str]:
	if not names or send_frequency not in (ONCE_PER_DAY, ONCE_PER_DOCUMENT):
		return set()
	filters = {
		"automation": automation_name,
		"reference_doctype": doctype,
		"reference_name": ("in", [str(n) for n in names]),
	}
	if send_frequency == ONCE_PER_DAY:
		filters["creation"] = (">=", nowdate())
	ran = {str(r) for r in frappe.get_all(RUN_DOCTYPE, filters={**filters, "status": ("in", ("Completed", "Waiting", "Running"))}, pluck="reference_name")}
	# a document that keeps failing would otherwise be "pending" again on every schedule tick
	failed = frappe.get_all(RUN_DOCTYPE, filters={**filters, "status": "Failed"},
		fields=["reference_name", {"COUNT": "name", "as": "failures"}], group_by="reference_name")
	return ran | {str(r.reference_name) for r in failed if r.failures >= MAX_FAILED_RUNS}


def iter_pending_names(automation_name, send_frequency, doctype, base_filters=None):
	last, scanned, queued = "", 0, 0
	while scanned < SCHEDULE_SCAN_LIMIT and queued < SCHEDULE_CEILING:
		names = frappe.get_all(doctype, filters=[*(base_filters or []), ["name", ">", last]],
			order_by="name asc", pluck="name", limit=SCHEDULE_BATCH)
		if not names:
			return
		last, scanned = names[-1], scanned + len(names)
		ran = _names_already_run(automation_name, send_frequency, doctype, names)
		pending = [n for n in names if str(n) not in ran]
		if pending:
			queued += len(pending)
			yield pending
	if queued >= SCHEDULE_CEILING:
		frappe.log_error(title=f"Messaging Automation {automation_name}: scheduled run hit {SCHEDULE_CEILING} documents")
	elif scanned >= SCHEDULE_SCAN_LIMIT:
		frappe.log_error(title=f"Messaging Automation {automation_name}: scheduled run hit scan limit of {SCHEDULE_SCAN_LIMIT} documents")


def _sent(log) -> int:
	return sum(1 for l in log if str(l.get("step_type", "")).startswith("Send") and l.get("result") == "sent")


def _json(raw, default):
	if not raw:
		return default
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return default


def trigger_row(automation, trigger_id, doctype):
	rows = automation.triggers or []
	if trigger_id:
		return next((t for t in rows if t.trigger_id == trigger_id), None)
	return next((t for t in rows if t.reference_doctype == doctype), None)  # jobs queued before the graph


def _passes(automation, trigger, doc) -> bool:
	if automation.skip_return_documents and doc.get("is_return") in (1, "1", True):
		return False
	rules = [f.as_dict() for f in (automation.filters or []) if f.trigger_id in ("", None, trigger.trigger_id)]
	if not rules_pass(rules, trigger.filter_logic or "All", doc):
		return False
	if trigger.condition and trigger.trigger_type != SCHEDULE:
		return bool(frappe.safe_eval(trigger.condition, get_safe_globals(), {"doc": doc.as_dict()}))
	return True


def effective_automation(automation, step, doctype):
	"""What the channel code sees for one node on one DocType: defaults + node overrides."""
	send_to = _json(step.get("send_to"), {}) or {}
	rec = next((r for r in (automation.get("recipients") or []) if r.reference_doctype == doctype), None)
	field = send_to.get(doctype) if isinstance(send_to, dict) else None
	phone_node = step.step_type in (SEND_WHATSAPP, SEND_SMS, ADD_TO_LIST)
	phone = (field if phone_node else None) or (rec.phone_field if rec else None) or automation.get("phone_field") or ""
	email = (field if step.step_type == SEND_EMAIL else None) or (rec.email_field if rec else None) or automation.get("email_field") or ""
	return frappe._dict(
		name=automation.name, send_frequency=automation.send_frequency, skip_return_documents=automation.skip_return_documents,
		phone_field=phone, email_field=email,
		whatsapp_account=step.get("whatsapp_account") or automation.whatsapp_account,
		whatsapp_phone=step.get("whatsapp_phone") or "",
		sms_account=step.get("sms_account") or automation.sms_account,
		variable_mappings=[m for m in (automation.variable_mappings or []) if (m.get("reference_doctype") or "") in ("", doctype)],
	)


def switch_handle(step, doc) -> str:
	actual = normalize(field_value(doc, step.switch_field)).lower()
	for i, case in enumerate(switch_cases(step), start=1):
		if normalize(case).lower() == actual:
			return f"case_{i}"
	return "otherwise"


def _node_executor(automation, graph, ctx, log):
	def execute(path, node_id):
		step = graph.steps[node_id]
		entry = {"path_id": path["path_id"], "node_id": node_id, "step_id": node_id, "step_type": step.step_type, "at": str(now_datetime())}
		log.append(entry)
		if step.step_type == WAIT:
			entry["result"] = f"waiting {step.wait_value} {step.wait_unit}"
			path["resume_at"] = add_to_date(now_datetime(), **{WAIT_UNITS.get(step.wait_unit, "days"): cint(step.wait_value)})
			return WAIT_SIGNAL
		if step.step_type == WAIT_EVENT:
			run_started_at = ctx.get("run_started_at") or now_datetime()
			if event_met(step, ctx.doc, run_started_at):
				entry["result"] = "event"
				return "event"
			entry["result"] = f"waiting for event, timeout {step.wait_value} {step.wait_unit}"
			path["resume_at"] = add_to_date(now_datetime(), **{WAIT_UNITS.get(step.wait_unit, "days"): cint(step.wait_value)})
			path["wait_for_event"] = 1
			path["wait_key"] = wait_key(step, ctx.doc)
			return WAIT_SIGNAL
		if step.step_type == IF_ELSE:
			entry["result"] = S.branch_of(ctx, step)
			return entry["result"].lower()
		if step.step_type == SWITCH:
			entry["result"] = switch_handle(step, ctx.doc)
			return entry["result"]
		node_ctx = frappe._dict(
			ctx,
			automation=effective_automation(automation, step, ctx.doc.doctype),
			retrying=cint(path.get("retry_count")) > 0,
			entry=entry,
		)
		try:
			entry["result"] = S.execute(node_ctx, step)
			path["retry_count"] = 0
		except Exception as e:
			delays = RETRY_DELAYS.get(e.kind, ()) if isinstance(e, channels.SendRetry) else ()
			tries = cint(path.get("retry_count"))
			if tries < len(delays):
				path["retry_count"] = tries + 1
				path["resume_at"] = add_to_date(now_datetime(), seconds=delays[tries])
				entry["result"] = f"retrying ({tries + 1}/{len(delays)}): {str(e)[:RETRY_NOTE_MAX]}"
				return WAIT_SIGNAL
			path["retry_count"] = 0
			entry["result"], entry["error"] = "error", str(e)[:RUN_ERROR_MAX]
			frappe.log_error(title=f"Messaging Automation {automation.name} step {node_id} failed")
		return ""
	return execute


def _persist(run, paths, log) -> None:
	last_sends = {}
	for entry in log:
		if str(entry.get("step_type", "")).startswith("Send"):
			node_key = (entry.get("path_id"), entry.get("node_id") or entry.get("step_id"))
			last_sends[node_key] = entry
	failed = bool(last_sends) and all(entry.get("result") == "error" for entry in last_sends.values())
	waiting = any(p["status"] == PATH_WAITING for p in paths)
	run.set("paths", [{"path_id": p["path_id"], "node_id": p["node_id"], "status": p["status"],
		"executed": 1 if p.get("executed") else 0, "joined": 1 if p.get("joined") else 0,
		"wait_for_event": 1 if p.get("wait_for_event") else 0,
		"wait_key": p.get("wait_key") or "",
		"retry_count": cint(p.get("retry_count", 0)),
		"resume_at": p.get("resume_at") if p["status"] == PATH_WAITING else None} for p in paths])
	run.status = "Waiting" if waiting else ("Failed" if failed else "Completed")
	run.log = json.dumps(log, default=str)
	run.sent_count = _sent(log)
	run.resume_step_id = None
	run.resume_at = min((get_datetime(p["resume_at"]) for p in paths if p["status"] == PATH_WAITING and p.get("resume_at")), default=None)
	run.flags.ignore_links = True
	run.save(ignore_permissions=True)


def _walk(automation, run, doc, ctx_data, paths):
	graph = Graph(automation.triggers, automation.steps, automation.edges)
	log = json.loads(run.log or "[]")
	ctx = frappe._dict(automation=automation, doc=doc, value=ctx_data.get("value") or "",
		discriminator=ctx_data.get("discriminator") or "", trigger_id=run.trigger_id, phone=ctx_data.get("phone"),
		run_started_at=getattr(run, "creation", None), run=getattr(run, "name", None))
	counter = itertools.count(len(paths) + 1)
	paths = advance(graph, paths, _node_executor(automation, graph, ctx, log), lambda: f"p{next(counter)}")
	log.extend({"path_id": p["path_id"], "node_id": p["node_id"], "step_id": p["node_id"], "result": "error", "error": p.pop("error"),
		"at": str(now_datetime())} for p in paths if p.get("error"))
	_persist(run, paths, log)


def run_automation(name, trigger_id, doctype, docname, value="", discriminator="", doc_dict=None, skip_checks=False, phone=None):
	run, claim_key, run_saved = None, None, False
	try:
		automation = frappe.get_doc(DOCTYPE, name)
		if automation.disabled:
			return None
		trigger = trigger_row(automation, trigger_id, doctype)
		doc = _load_doc(doctype, docname, doc_dict)
		if trigger is None or doc is None or (not skip_checks and not _passes(automation, trigger, doc)):
			return None
		ref_name = docname or getattr(doc, "name", None)
		if not skip_checks and _already_ran(name, automation.send_frequency, doctype, ref_name):
			return None
		# _already_ran is check-then-insert: two jobs for the same doc (e.g.
		# after_insert + an immediate on_update) could both pass it and send
		# twice (audit H7). Claim the (automation, doc[, day]) slot atomically
		# until this job's Automation Run row is committed — after that,
		# _already_ran itself catches later jobs.
		if not skip_checks and ref_name and automation.send_frequency in (ONCE_PER_DAY, ONCE_PER_DOCUMENT):
			from msuite_automation.msuite_automation.engine.channels import LockUnavailable, acquire_lock

			slot = nowdate() if automation.send_frequency == ONCE_PER_DAY else "doc"
			key = f"ma_run_claim:{name}:{doctype}:{ref_name}:{slot}"
			try:
				claimed = acquire_lock(key, ttl=RUN_CLAIM_TTL_SECONDS)
			except LockUnavailable as e:
				frappe.logger(LOGGER_NAME).warning(f"run_automation: claim unavailable for {name} on {doctype} {ref_name}, run skipped: {e}")
				return None
			if not claimed:
				return None
			claim_key = key
		ctx_data = {"value": value, "discriminator": discriminator, "doc_dict": doc_dict, "phone": phone}
		run = frappe.get_doc({"doctype": RUN_DOCTYPE, "automation": name, "trigger_id": trigger.trigger_id, "status": "Running",
			"reference_doctype": doctype, "reference_name": ref_name, "context": json.dumps(ctx_data, default=str),
			"log": json.dumps([{"path_id": "p1", "node_id": trigger.trigger_id, "step_id": trigger.trigger_id, "step_type": "Trigger", "result": "started", "at": str(now_datetime())}])})
		run.insert(ignore_permissions=True, ignore_links=True)
		run_saved = True
		_walk(automation, run, doc, ctx_data, start_paths(trigger.trigger_id))
		return run.name
	except Exception:
		if claim_key and not run_saved:
			channels.release_lock(claim_key)  # no run row exists to stop a re-trigger, so do not block it for the claim TTL
		if run and getattr(run, "name", None):
			_mark_failed(run.name, "executor")
		frappe.log_error(title=f"Messaging Automation run failed: {name} on {doctype} {docname}")
		return None


def _mark_failed(run_name, where):
	try:
		log_list = json.loads(frappe.db.get_value(RUN_DOCTYPE, run_name, "log") or "[]")
	except Exception:
		log_list = []
	tb = frappe.get_traceback(with_context=False)
	error = tb if tb and "NoneType: None" not in tb else f"{where}: worker lost"
	log_list.append({"step_id": where, "step_type": "Run", "result": "error", "error": error[:RUN_ERROR_MAX], "at": str(now_datetime())})
	frappe.db.set_value(RUN_DOCTYPE, run_name, {"status": "Failed", "resume_step_id": None, "resume_at": None, "log": json.dumps(log_list)}, update_modified=False)
	frappe.db.sql("update `tabAutomation Run Path` set status='Ended' where parent=%s and status!='Ended'", run_name)


def _due_run_names() -> list[str]:
	now = now_datetime()
	graph_runs = frappe.db.sql_list("""select distinct p.parent from `tabAutomation Run Path` p
		join `tabMessaging Automation Run` r on r.name = p.parent
		where p.parenttype=%s and p.status='Waiting' and p.resume_at <= %s and r.status='Waiting' limit %s""", (RUN_DOCTYPE, now, DUE_BATCH))
	legacy = frappe.db.sql_list("""select r.name from `tabMessaging Automation Run` r where r.status='Waiting' and r.resume_at <= %s
		and ifnull(r.resume_step_id, '') != ''
		and not exists (select 1 from `tabAutomation Run Path` p where p.parent = r.name and p.parenttype=%s) limit %s""", (now, RUN_DOCTYPE, DUE_BATCH))
	return list(dict.fromkeys(graph_runs + legacy))


def _event_run_names() -> list[str]:
	return frappe.db.sql_list("""select r.name from `tabMessaging Automation Run` r
		where r.status='Waiting' and exists (
			select 1 from `tabAutomation Run Path` p
			where p.parent=r.name and p.parenttype=%s and p.status='Waiting' and ifnull(p.wait_for_event,0)=1)
		order by r.modified asc limit %s""", (RUN_DOCTYPE, DUE_BATCH))


def _load_resume_context(run):
	automation = frappe.get_doc(DOCTYPE, run.automation) if frappe.db.exists(DOCTYPE, run.automation) else None
	ctx_data = json.loads(run.context or "{}")
	rt, rn = run.reference_doctype, run.reference_name
	doc = frappe.get_doc(rt, rn) if (rt and rn and frappe.db.exists(rt, rn)) else _load_doc(rt, rn, ctx_data.get("doc_dict"))
	return automation, doc, ctx_data


def resume_waiting_runs(run_names: list | None = None) -> None:
	now = now_datetime()
	not_met_runs = []
	names_to_check = run_names if run_names is not None else _due_run_names()
	for run_name in names_to_check:
		try:
			run = frappe.get_doc(RUN_DOCTYPE, run_name) if frappe.db.exists(RUN_DOCTYPE, run_name) else None
			if not run or run.status != "Waiting":
				continue

			waiting_paths = [p for p in (run.paths or []) if p.status == PATH_WAITING]
			has_due_path = any(p.resume_at and get_datetime(p.resume_at) <= now for p in waiting_paths)
			if run.paths and not has_due_path:
				event_paths = [p for p in waiting_paths if getattr(p, "wait_for_event", 0)]
				if not event_paths:
					continue
				automation, doc, _ = _load_resume_context(run)
				doc_deleted = bool(
					run.reference_doctype
					and run.reference_name
					and not frappe.db.exists(run.reference_doctype, run.reference_name)
				)
				if automation and not automation.disabled and doc and not doc_deleted:
					graph = Graph(automation.triggers, automation.steps, automation.edges)
					is_met = any(
						event_met(graph.steps.get(p.node_id), doc, run.creation)
						for p in event_paths
						if graph.steps.get(p.node_id)
					)
					if not is_met:
						not_met_runs.append(run_name)
						continue

			# Claim atomically: only the tick that flips Waiting→Running continues it.
			frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set status='Running', modified=%s where name=%s and status='Waiting'", (now_datetime(), run_name))
			if not frappe.db.sql("select row_count()")[0][0]:
				continue
			frappe.db.commit()
			run = frappe.get_doc(RUN_DOCTYPE, run_name)
			automation, doc, ctx_data = _load_resume_context(run)
			if not automation or automation.disabled or doc is None:
				run.db_set({"status": "Skipped", "resume_at": None})
				frappe.db.commit()
				continue
			if run.paths:
				paths = [{"path_id": p.path_id, "node_id": p.node_id, "status": p.status, "executed": bool(p.executed),
					"joined": bool(p.joined), "handle": "", "resume_at": p.resume_at,
					"wait_for_event": bool(getattr(p, "wait_for_event", 0)),
					"wait_key": getattr(p, "wait_key", "") or "",
					"retry_count": cint(getattr(p, "retry_count", 0))} for p in run.paths]
				if any(p["wait_for_event"] for p in paths) and not (
					run.reference_doctype and run.reference_name and frappe.db.exists(run.reference_doctype, run.reference_name)
				):  # the document was deleted while the path waited for an event on it
					run.db_set({"status": "Skipped", "resume_at": None})
					frappe.db.commit()
					continue
				any_changed = False
				graph = Graph(automation.triggers, automation.steps, automation.edges)
				for p in paths:
					if p["status"] == PATH_WAITING:
						if p.get("wait_for_event"):
							step = graph.steps.get(p["node_id"])
							if step and event_met(step, doc, run.creation):
								p["status"] = PATH_RUNNING
								p["handle"] = "event"
								p["executed"] = True
								p["wait_for_event"] = False
								any_changed = True
							elif p.get("resume_at") and get_datetime(p["resume_at"]) <= now:
								p["status"] = PATH_RUNNING
								p["handle"] = "timeout"
								p["executed"] = True
								p["wait_for_event"] = False
								any_changed = True
						else:
							if p.get("resume_at") and get_datetime(p["resume_at"]) <= now:
								p["status"] = PATH_RUNNING
								if p.get("retry_count", 0) > 0:
									p["executed"] = False
								any_changed = True
				if not any_changed:
					run.db_set({"status": "Waiting"})
					frappe.db.commit()
					continue
			else:  # parked by the tree engine: resume_step_id = next node to execute
				graph_ids = {s.step_id for s in automation.steps}
				if run.resume_step_id not in graph_ids:
					run.db_set({"status": "Skipped", "resume_at": None})
					frappe.db.commit()
					continue
				paths = [{"path_id": "p1", "node_id": run.resume_step_id, "status": PATH_RUNNING, "executed": False, "joined": True, "handle": "", "wait_key": ""}]
			_walk(automation, run, doc, ctx_data, paths)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			try:
				_mark_failed(run_name, "resume")
				frappe.db.commit()
			except Exception:
				pass
			frappe.log_error(title=f"Messaging Automation Run resume failed: {run_name}")

	if not_met_runs:
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name in %s and status='Waiting'", (now_datetime(), tuple(not_met_runs)))
		frappe.db.commit()


def poll_event_waits() -> None:
	names = _event_run_names()
	if names:
		resume_waiting_runs(run_names=names)


def check_event_waits(doctype: str, name: str) -> None:
	if not doctype or not name:
		return
	if not frappe.db.exists(doctype, name):
		return
	doc = frappe.get_doc(doctype, name)
	candidate_run_names: list[str] = []

	# 1. Same-doc mode: Waiting runs on this doc having a Waiting wait_for_event path
	same_doc_runs = frappe.db.sql_list("""
		select distinct r.name
		from `tabMessaging Automation Run` r
		join `tabAutomation Run Path` p on p.parent = r.name
		where r.status = 'Waiting'
		  and r.reference_doctype = %s
		  and r.reference_name = %s
		  and p.parenttype = %s
		  and p.status = 'Waiting'
		  and ifnull(p.wait_for_event, 0) = 1
	""", (doctype, name, RUN_DOCTYPE))
	candidate_run_names.extend(same_doc_runs)

	# 2. Linked mode: for each linked watch of that doctype
	from msuite_automation.msuite_automation.engine.triggers import get_watch_map
	from msuite_automation.msuite_automation.engine.event_check import reader_wait_keys

	watches = get_watch_map().get(doctype, [])
	linked_watches = [w for w in watches if len(w) >= 4 and w[2] == "linked"]
	by_field: dict[str, set[str]] = {}
	for auto_name, _sid, _mode, link_field in linked_watches:
		if link_field:
			by_field.setdefault(link_field, set()).add(auto_name)

	for link_field, auto_names in by_field.items():
		val = field_value(doc, link_field)
		keys = reader_wait_keys(doctype, link_field, val)
		if not keys:
			continue
		linked_runs = frappe.db.sql_list("""
			select distinct r.name
			from `tabMessaging Automation Run` r
			join `tabAutomation Run Path` p on p.parent = r.name
			where r.status = 'Waiting'
			  and r.automation in %(automations)s
			  and p.parenttype = %(pt)s
			  and p.status = 'Waiting'
			  and ifnull(p.wait_for_event, 0) = 1
			  and p.wait_key in %(keys)s
		""", {"automations": tuple(auto_names), "pt": RUN_DOCTYPE, "keys": tuple(keys)})
		candidate_run_names.extend(linked_runs)

	all_candidates = list(dict.fromkeys(candidate_run_names))
	if len(all_candidates) > EVENT_CHECK_CAP:
		dropped = len(all_candidates) - EVENT_CHECK_CAP
		frappe.logger(LOGGER_NAME).warning(
			f"check_event_waits: candidate list truncated ({dropped} candidates dropped); 10-minute poll will pick them up"
		)
	deduped = all_candidates[:EVENT_CHECK_CAP]
	if deduped:
		resume_waiting_runs(run_names=deduped)


def sweep_stuck_runs() -> None:
	"""Fail runs whose worker died mid-walk. They would otherwise stay Running forever and,
	because _already_ran counts Running, block the once-per-document/day check for that record."""
	cutoff = add_to_date(now_datetime(), minutes=-STUCK_AFTER_MINUTES)
	for name in frappe.db.sql_list(f"select name from `tab{RUN_DOCTYPE}` where status='Running' and modified < %s limit %s", (cutoff, STUCK_SWEEP_BATCH)):
		try:
			# re-check inside the UPDATE so a worker that just finished is not overwritten
			frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set status='Failed' where name=%s and status='Running' and modified < %s", (name, cutoff))
			if frappe.db.sql("select row_count()")[0][0]:
				_mark_failed(name, "worker lost")
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Messaging Automation Run sweep failed: {name}")


def run_scheduled(name, trigger_id=None) -> None:
	"""Schedule trigger. With a condition script: legacy script mode
	(`_contact_list` / `_data_list`). Otherwise: every doc of the trigger's DocType."""
	automation = frappe.get_doc(DOCTYPE, name)
	trigger = trigger_row(automation, trigger_id, None) or next((t for t in automation.triggers if t.trigger_type == SCHEDULE), None)
	if trigger is None:
		return
	if trigger.condition:
		scope = frappe._dict(doc=frappe._dict(automation.as_dict()))
		safe_exec(trigger.condition, get_safe_globals(), scope)
		holder = scope.doc
		for data in holder.get("_data_list") or []:
			enqueue_run(name=name, trigger_id=trigger.trigger_id, doctype=trigger.reference_doctype, docname=data.get("name"), skip_checks=True, phone=data.get("phone_no"))
		if holder.get("_contact_list"):
			from msuite_automation.msuite_automation.engine import channels

			graph = Graph(automation.triggers, automation.steps, automation.edges)
			wa = [graph.steps[n] for n in graph.targets(trigger.trigger_id) if graph.steps[n].step_type == SEND_WHATSAPP]
			for phone in holder._contact_list:
				for s in wa:
					try:
						channels.send_whatsapp(effective_automation(automation, s, trigger.reference_doctype), s, frappe._dict(doctype=None, name=None), phone=channels.format_number(phone))
					except channels.RecipientOptedOut:
						continue
					except Exception:
						frappe.log_error(title=f"Messaging Automation {name}: scheduled send to {phone} failed")
		return
	if not trigger.reference_doctype:
		return
	enqueue_pending(name, trigger.trigger_id, trigger.reference_doctype, automation.send_frequency)
