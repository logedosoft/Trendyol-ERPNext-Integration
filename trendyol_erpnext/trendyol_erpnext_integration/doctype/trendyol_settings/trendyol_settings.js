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

    test_pdf_sales_order(frm) {
        if (!frm.doc.test_pdf_sales_order) return;
        frappe.call({
            method: 'trendyol_erpnext.utils.test_send_invoice_pdf',
            args: { strSalesOrderName: frm.doc.test_pdf_sales_order },
            freeze: true,
            freeze_message: __('Sending invoice PDF to Trendyol...'),
            callback(r) {
                if (r.exc || !r.message) return;
                _show_invoice_steps_dialog(r.message, "Invoice PDF Upload Successful", "Invoice PDF Upload Failed");
            },
        });
    },

    test_pdf_trendyol_order(frm) {
        if (!frm.doc.test_pdf_trendyol_order) return;
        frappe.call({
            method: 'trendyol_erpnext.utils.test_send_invoice_pdf_by_trendyol_order',
            args: { strTrendyolOrderName: frm.doc.test_pdf_trendyol_order },
            freeze: true,
            freeze_message: __('Sending invoice PDF to Trendyol...'),
            callback(r) {
                if (r.exc || !r.message) return;
                _show_invoice_steps_dialog(r.message, "Invoice PDF Upload Successful", "Invoice PDF Upload Failed");
            },
        });
    },

    delete_trendyol_invoice(frm) {
        if (!frm.doc.delete_invoice_pdf_in_trendyol) return;
        frappe.call({
            method: 'trendyol_erpnext.utils.delete_trendyol_invoice',
            args: { strTrendyolOrderName: frm.doc.delete_invoice_pdf_in_trendyol },
            freeze: true,
            freeze_message: __('Deleting invoice from Trendyol...'),
            callback(r) {
                if (r.exc || !r.message) return;
                _show_invoice_steps_dialog(r.message, "Invoice Deleted from Trendyol", "Invoice Delete Failed", function () {
                    frm.reload_doc();
                });
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

        frm.add_custom_button(__('Delete Invoice from Trendyol'), () => {
            frm.trigger('delete_trendyol_invoice');
        }, __('Actions'));
    },
});
