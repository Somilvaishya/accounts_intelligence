const ADMIN = "accounts_intelligence.auth.admin";
const STATUS_COLOUR = {
	Connected: "green",
	"Waiting for approval": "orange",
	"Not connected": "gray",
	"Not authorised": "red",
	Error: "red",
};

frappe.ui.form.on("Accounts Intelligence Settings", {
	refresh(frm) {
		frm.set_intro("");
		if (!frm.doc.mode) {
			frm.set_intro(__("Choose this site's mode and save. A key pair for this site is created on the first save."), "blue");
			return;
		}
		if (frm.doc.mode === "Client") setup_client(frm);
		if (frm.doc.mode === "Server") setup_server(frm);
		setup_key_buttons(frm);
	},
});

function setup_key_buttons(frm) {
	const group = __("Key");
	if (frm.doc.mode === "Client" && frm.doc.server_public_key && frm.doc.connection_status === "Connected") {
		frm.add_custom_button(
			__("Rotate Key"),
			() => {
				frappe.confirm(
					__(
						"Create a new key for this site and switch to it? The server checks the new key at this site's address and keeps the approval. If it is interrupted, click Rotate Key again."
					),
					() => run(frm, "client_rotate_key").then(show_key_result)
				);
			},
			group
		);
	}
	frm.add_custom_button(
		__("Regenerate Key"),
		() => {
			const consequence =
				frm.doc.mode === "Client"
					? __("The server stops accepting this site until its admin resets this site's row and approves it again. Use Rotate Key instead unless the current key may be known to someone else.")
					: __("Every client must click Reset Connection, check the new fingerprint and connect again. Their approvals stay valid.");
			frappe.confirm(`<p>${__("Replace this site's key pair now?")}</p><p>${consequence}</p>`, () =>
				run(frm, "regenerate_key").then(show_key_result)
			);
		},
		group
	);
}

function show_key_result(result) {
	if (!result) return;
	if (!result.ok) {
		frappe.msgprint({ title: __("Key not changed"), message: frappe.utils.escape_html(result.message), indicator: "red" });
		return;
	}
	const fingerprint = `<p>${__("New fingerprint")}: <code>${frappe.utils.escape_html(result.own_fingerprint || result.fingerprint || "")}</code></p>`;
	const message = result.message ? `<p>${frappe.utils.escape_html(result.message)}</p>` : `<p>${__("The server accepted the new key. This site stays connected.")}</p>`;
	frappe.msgprint({ title: __("Key replaced"), message: message + fingerprint, indicator: "green" });
}

function setup_client(frm) {
	const status = frm.doc.connection_status || "Not connected";
	let intro = __("Connection: {0}", [`<b>${frappe.utils.escape_html(__(status))}</b>`]);
	if (frm.doc.last_error) intro += `<br>${frappe.utils.escape_html(frm.doc.last_error)}`;
	frm.set_intro(intro, STATUS_COLOUR[status] || "gray");

	frm.add_custom_button(__("Connect to Server"), () => {
		run(frm, "client_connect").then(show_client_result);
	});
	if (frm.doc.server_public_key) {
		frm.add_custom_button(__("Test Connection"), () => {
			run(frm, "client_test").then(show_client_result);
		});
		frm.add_custom_button(__("Reset Connection"), () => {
			frappe.confirm(
				__("Forget the saved server key? Click Connect to Server afterwards. The server's approval stays valid as long as this site's own key is unchanged."),
				() => run(frm, "client_reset")
			);
		});
	}
}

function show_client_result(result) {
	if (!result) return;
	if (!result.ok) {
		frappe.msgprint({ title: __("Not connected"), message: frappe.utils.escape_html(result.message), indicator: "red" });
		return;
	}
	const fingerprints = `
		<p>${__("Both admins should see the same two fingerprints:")}</p>
		<p>${__("This site")}: <code>${frappe.utils.escape_html(result.own_fingerprint || "")}</code><br>
		${__("Server")}: <code>${frappe.utils.escape_html(result.server_fingerprint || "")}</code></p>`;
	const message =
		result.status === "Connected"
			? `<p>${__("The server accepted this site.")}</p>`
			: `<p>${__("The server has the request. Its admin now has to approve this site.")}</p>${fingerprints}`;
	frappe.msgprint({ title: __(result.status), message, indicator: STATUS_COLOUR[result.status] || "blue" });
}

function setup_server(frm) {
	const rows = frm.doc.allowed_sites || [];
	const waiting = rows.filter((r) => r.status === "Pending" && r.public_key).length;
	const active = rows.filter((r) => r.status === "Active").length;
	frm.set_intro(
		__("{0} connected, {1} waiting for approval. This site's fingerprint: {2}", [
			active,
			waiting,
			`<code>${frappe.utils.escape_html(frm.doc.key_fingerprint || "")}</code>`,
		]),
		waiting ? "orange" : "blue"
	);

	const grid = frm.fields_dict.allowed_sites.grid;
	grid.add_custom_button(__("Approve Selected"), () => act(frm, "server_approve", __("Approve")));
	grid.add_custom_button(__("Revoke Selected"), () => act(frm, "server_revoke", __("Revoke")));
	grid.add_custom_button(__("Reset Selected"), () => act(frm, "server_reset", __("Reset")));
}

function act(frm, method, label) {
	const rows = frm.fields_dict.allowed_sites.grid.get_selected_children();
	if (!rows.length) {
		frappe.msgprint(__("Select one or more sites first."));
		return;
	}
	const lines = rows
		.map((r) => `<li>${frappe.utils.escape_html(r.site_url)}: <code>${frappe.utils.escape_html(r.fingerprint || __("no key yet"))}</code></li>`)
		.join("");
	const notes = {
		server_approve: __("Approve only if each fingerprint matches the one shown on that site's settings page."),
		server_revoke: __("Revoked sites are refused at once. To let one back in, reset it and approve it again."),
		server_reset: __("The site's key is forgotten. It must connect and be approved again."),
	};
	frappe.confirm(`<p>${label}:</p><ul>${lines}</ul><p>${notes[method]}</p>`, () => {
		run(frm, method, { rows: rows.map((r) => r.name) });
	});
}

function run(frm, method, args) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the settings first."));
		return Promise.resolve();
	}
	return frappe
		.call({ method: `${ADMIN}.${method}`, args: args || {}, freeze: true })
		.then((r) => {
			frm.reload_doc();
			return r.message;
		});
}
