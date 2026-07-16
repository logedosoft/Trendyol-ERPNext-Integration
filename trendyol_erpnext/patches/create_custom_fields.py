def execute():
	from trendyol_erpnext.install import get_custom_fields
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
	create_custom_fields(get_custom_fields(), ignore_validate=True)
