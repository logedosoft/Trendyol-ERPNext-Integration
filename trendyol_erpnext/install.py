import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def get_custom_fields():
	return {
		"Customer": [
			{
				"fieldname": "trendyol_customer_id",
				"fieldtype": "Data",
				"label": "Trendyol Customer ID",
				"insert_after": "dn_required",
			}
		],
	}


def after_install():
	run_install_setup()


def after_sync():
	run_install_setup()


def before_uninstall():
	pass


def run_install_setup():
	create_custom_fields(get_custom_fields(), ignore_validate=True)
	frappe.db.commit()
