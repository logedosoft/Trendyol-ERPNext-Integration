function _show_invoice_steps_dialog(result, strSuccessTitle, strFailureTitle, callback) {
	if (!result || !result.steps) return;

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
		title: result.op_result ? __(strSuccessTitle) : __(strFailureTitle),
		fields: [
			{
				fieldtype: "HTML",
				fieldname: "steps_html",
				options: step_html
			}
		],
		primary_action_label: __("Close"),
		primary_action: function () {
			dialog.hide();
			if (callback) callback();
		}
	});

	dialog.show();
}
