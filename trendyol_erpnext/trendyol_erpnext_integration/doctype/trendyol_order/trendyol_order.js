frappe.ui.form.on('Trendyol Order', {
    refresh(frm) {
        if (!frm.doc.__islocal
            && frm.doc.status !== 'Completed'
            && frm.doc.status !== 'Processing') {
            frm.add_custom_button(__('Sales Order'), () => {
                frappe.call({
                    method: 'trendyol_erpnext.utils.create_sales_order_from_trendyol_order',
                    args: { strOrderName: frm.doc.name },
                    freeze: true,
                    freeze_message: __('Creating Sales Order...'),
                    callback(r) {
                        if (r.message && r.message.op_result) {
                            frappe.show_alert({
                                message: r.message.op_message,
                                indicator: 'green',
                            }, 5);
                            frm.reload_doc();
                        } else if (r.message) {
                            frappe.msgprint({
                                message: r.message.op_message,
                                title: __('Sales Order Creation'),
                                indicator: 'red',
                            });
                        }
                    },
                });
            }, __('Create'));
        }

        if (!frm.doc.__islocal && frm.doc.sales_order && !frm.doc.invoice_sent) {
            frm.add_custom_button(__('Send Invoice PDF to Trendyol'), () => {
                frappe.call({
                    method: 'trendyol_erpnext.utils.test_send_invoice_pdf_by_trendyol_order',
                    args: { strTrendyolOrderName: frm.doc.name },
                    freeze: true,
                    freeze_message: __('Sending invoice PDF to Trendyol...'),
                    callback(r) {
                        if (r.exc || !r.message) return;
                        _show_invoice_steps_dialog(r.message, "Invoice PDF Upload Successful", "Invoice PDF Upload Failed", function () {
                            frm.reload_doc();
                        });
                    },
                });
            }, __('Actions'));
        }

        if (!frm.doc.__islocal && frm.doc.invoice_sent) {
            frm.add_custom_button(__('Delete Invoice PDF from Trendyol'), () => {
                frappe.call({
                    method: 'trendyol_erpnext.utils.delete_trendyol_invoice',
                    args: { strTrendyolOrderName: frm.doc.name },
                    freeze: true,
                    freeze_message: __('Deleting invoice from Trendyol...'),
                    callback(r) {
                        if (r.exc || !r.message) return;
                        _show_invoice_steps_dialog(r.message, "Invoice Deleted from Trendyol", "Invoice Delete Failed", function () {
                            frm.reload_doc();
                        });
                    },
                });
            }, __('Actions'));
        }
    },
});
