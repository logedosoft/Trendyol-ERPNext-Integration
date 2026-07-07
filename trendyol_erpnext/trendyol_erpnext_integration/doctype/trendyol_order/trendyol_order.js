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
                        }
                    },
                });
            }, __('Create'));
        }
    },
});
