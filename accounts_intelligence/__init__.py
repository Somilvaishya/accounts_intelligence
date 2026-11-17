__version__ = "0.0.1"


def check_app_permission() -> bool:
	"""The apps screen's hook for this app's tile; the rule is in roles.py."""
	from accounts_intelligence.roles import check_app_permission

	return check_app_permission()
