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

                var result = r.message;

                var step_html = "<div style='max-height: 400px; overflow-y: auto;'>";
                step_html += "<table class='table table-bordered' style='margin: 0;'>";
                step_html += "<thead><tr><th>" + __("Step") + "</th><th>" + __("Status") + "</th><th>" + __("Message") + "</th></tr></thead>";
                step_html += "<tbody>";

                for (var i = 0; i < result.steps.length; i++) {
                    var step = result.steps[i];
                    var status_icon;

                    if (step.status === "success") {
                        status_icon = "<span style='color: green;'>✓</span>";
                    } else if (step.status === "error") {
                        status_icon = "<span style='color: red;'>✗</span>";
                    } else {
                        status_icon = "<span style='color: blue;'>ℹ</span>";
                    }

                    step_html += "<tr>";
                    step_html += "<td>" + step.step + "</td>";
                    step_html += "<td style='text-align: center;'>" + status_icon + "</td>";
                    step_html += "<td>" + step.message + "</td>";
                    step_html += "</tr>";
                }

                step_html += "</tbody></table></div>";

                var dialog = new frappe.ui.Dialog({
                    title: result.op_result
                        ? __("Invoice PDF Upload Successful")
                        : __("Invoice PDF Upload Failed"),
                    fields: [
                        {
                            fieldtype: "HTML",
                            fieldname: "steps_html",
                            options: step_html
                        }
                    ],
                    primary_action_label: __("Close"),
                    primary_action: function() {
                        dialog.hide();
                    }
                });

                dialog.show();
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
