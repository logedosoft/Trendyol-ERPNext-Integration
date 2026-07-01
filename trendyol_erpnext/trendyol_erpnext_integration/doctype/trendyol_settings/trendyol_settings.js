frappe.ui.form.on('Trendyol Settings', {
    btn_check_connection(frm) {
        frappe.call({
            method: 'trendyol_erpnext.utils.check_connection',
            args: { docname: frm.doc.name },
            freeze: true,
            freeze_message: __('Checking connection to Trendyol...'),
            callback(r) {
                if (r.message && r.message.op_result) {
                    frappe.show_alert({
                        message: r.message.op_message,
                        indicator: 'green',
                    }, 5);
                } else if (r.message) {
                    frappe.msgprint({
                        message: r.message.op_message,
                        title: __('Connection Failed'),
                        indicator: 'red',
                    });
                }
            },
        });
    },

    refresh(frm) {
        frm.add_custom_button(__('Fetch Orders'), () => {
            frappe.call({
                method: 'trendyol_erpnext.utils.poll_orders',
                freeze: true,
                freeze_message: __('Fetching orders from Trendyol...'),
                callback(r) {
                    if (r.message && r.message.op_result) {
                        frappe.show_alert({
                            message: r.message.op_message,
                            indicator: 'green',
                        }, 5);
                    } else if (r.message) {
                        frappe.msgprint({
                            message: r.message.op_message,
                            title: __('Fetch Orders'),
                            indicator: 'red',
                        });
                    }
                },
            });
        }, __('Actions'));
    },
});
