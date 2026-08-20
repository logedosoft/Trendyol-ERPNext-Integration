frappe.listview_settings['Trendyol Order'] = {
    onload: function(listview) {
        listview.page.add_action_item(__('Send Invoice PDF to Trendyol'), function() {
            listview.call_for_selected_items(
                "trendyol_erpnext.utils.bulk_send_invoice_to_trendyol",
                {doctype: "Trendyol Order"}
            );

            frappe.realtime.off("trendyol_invoice_progress");
            frappe.realtime.off("trendyol_invoice_done");

            frappe.realtime.on("trendyol_invoice_progress", function(data) {
                frappe.show_progress(__("Sending Invoices"), data.current, data.total, "Please wait...");
            });

            frappe.realtime.on("trendyol_invoice_done", function(data) {
                frappe.hide_progress();

                let msg = `<b>${__('Invoice Upload Complete')}</b><br>`;
                msg += `${__('Success')}: ${data.success}<br>`;
                msg += `${__('Failed')}: ${data.failed}<br>`;
                msg += `${__('Skipped')}: ${data.skipped}`;

                if (data.errors && data.errors.length > 0) {
                    msg += "<hr>" + data.errors.slice(0, 10).join("<br>");
                    if (data.errors.length > 10) msg += "<br>...and more errors.";
                }

                frappe.msgprint({
                    title: __("Trendyol Invoice Result"),
                    message: msg,
                    indicator: data.failed === 0 ? 'green' : 'orange',
                    wide: true
                });
            });
        }, __('Actions'), 'octicon octicon-upload');

        listview.page.add_action_item(__('Delete Invoice PDF from Trendyol'), function() {
            listview.call_for_selected_items(
                "trendyol_erpnext.utils.bulk_delete_invoice_from_trendyol",
                {doctype: "Trendyol Order"}
            );

            frappe.realtime.off("trendyol_invoice_progress");
            frappe.realtime.off("trendyol_invoice_done");

            frappe.realtime.on("trendyol_invoice_progress", function(data) {
                frappe.show_progress(__("Deleting Invoices"), data.current, data.total, "Please wait...");
            });

            frappe.realtime.on("trendyol_invoice_done", function(data) {
                frappe.hide_progress();

                let msg = `<b>${__('Invoice Delete Complete')}</b><br>`;
                msg += `${__('Success')}: ${data.success}<br>`;
                msg += `${__('Failed')}: ${data.failed}<br>`;
                msg += `${__('Skipped')}: ${data.skipped}`;

                if (data.errors && data.errors.length > 0) {
                    msg += "<hr>" + data.errors.slice(0, 10).join("<br>");
                    if (data.errors.length > 10) msg += "<br>...and more errors.";
                }

                frappe.msgprint({
                    title: __("Trendyol Invoice Result"),
                    message: msg,
                    indicator: data.failed === 0 ? 'green' : 'orange',
                    wide: true
                });
            });
        }, __('Actions'), 'octicon octicon-trash');
    }
};