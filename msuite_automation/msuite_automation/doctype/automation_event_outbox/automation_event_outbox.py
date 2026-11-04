from frappe.model.document import Document


class AutomationEventOutbox(Document):
	"""A trigger event saved in the same transaction as its document, until a worker claims it.
	Deleting the row is the claim (see executor.deliver_outbox)."""
