frappe.listview_settings['Trendyol Order'] = {
    onload: function(listview) {
        // Add a custom button to the list view
        listview.page.add_action_item(__('Delete Sales Invoice PDF from Trendyol'), function() {
            const selected = listview.get_checked_items();
            if (!selected || selected.length === 0) {
                frappe.msgprint(__('Please select at least one Trendyol Order.'));
                return;
            }

            frappe.call({
                method: 'trendyol_erpnext.utils.bulk_delete_invoice_from_trendyol',
                args: { order_names: selected },
                freeze: true,
                freeze_message: __('Deleting invoice PDFs from Trendyol...'),
                callback: function(r) {
                    if (r.message) {
                        frappe.show_alert({
                            message: r.message.op_message,
                            indicator: r.message.op_result ? 'green' : 'red'
                        }, 5);
                        listview.refresh();
                    }
                }
            });
        }, __('Actions'), 'octicon octicon-trash');
    }
};