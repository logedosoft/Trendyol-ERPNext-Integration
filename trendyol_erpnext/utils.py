import json
from datetime import datetime, timedelta, timezone

import frappe
from frappe.utils import cint, flt, rounded
import requests
from requests.auth import HTTPBasicAuth

MAX_POLL_PAGES = 10
MAX_WINDOW_DAYS = 14
GMT3 = timezone(timedelta(hours=3))
TRENDYOL_STATUSES_TO_SKIP = {"Awaiting", "Created"}


def _log_trendyol_call(docSettings, strLogTitle, strMethod, strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dStatusCode=None, strResponseBody=None):
    """Log a Trendyol API call to the Error Log when logging is enabled."""
    if docSettings.enable_logging:
        strTitle = strLogTitle
        lstParts = [
            f"Method: {strMethod}",
            f"URL: {strUrl}",
            f"API Key: {strApiKey}",
            f"API Secret: {'*' * 8}",
            f"Headers: {dctHeaders}",
            f"Params: {dctParams}",
            f"Status: {dStatusCode or 'Network error — request never completed'}",
        ]
        if strResponseBody:
            lstParts.append(f"Response: {strResponseBody[:2000]}")
        frappe.log_error(title=strTitle, message="\n".join(lstParts))


def _build_trendyol_request(docSettings, dctParams):
    """Build the URL, headers, and auth tuple for a Trendyol API request."""
    strSupplierId = docSettings.supplier_id
    strApiKey = docSettings.get_password("api_key")
    strApiSecret = docSettings.get_password("api_secret")
    strBaseUrl = docSettings.service_url.rstrip("/")
    strUrl = f"{strBaseUrl}/integration/order/sellers/{strSupplierId}/orders"
    dctHeaders = {
        "User-Agent": f"{strSupplierId} - ERPNextIntegration",
        "Accept": "*/*",
    }
    return strUrl, dctParams, dctHeaders, strApiKey, strApiSecret


def _to_epoch_ms(dt):
    """Convert a datetime to epoch milliseconds in GMT+3."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=GMT3)
    return int(dt.timestamp() * 1000)


def _acquire_poll_lock(strCompany):
    """Acquire a Redis cache lock for polling. Returns True if acquired."""
    strLockKey = f"trendyol_poll_lock_{strCompany}"
    blnAlreadyRunning = frappe.cache().get_value(strLockKey)
    if blnAlreadyRunning:
        return False
    frappe.cache().set_value(strLockKey, True, expires_in_sec=1200)
    return True


def _release_poll_lock(strCompany):
    """Release the Redis cache lock for polling."""
    strLockKey = f"trendyol_poll_lock_{strCompany}"
    frappe.cache().delete_value(strLockKey)


@frappe.whitelist()
def check_connection(docname):
    """Verify connectivity and authentication against the Trendyol API."""
    docSettings = frappe.get_doc("Trendyol Settings", docname)
    dtNow = datetime.now(GMT3)
    dtYesterday = dtNow - timedelta(days=1)
    dctParams = {
        "startDate": _to_epoch_ms(dtYesterday),
        "endDate": _to_epoch_ms(dtNow),
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "ASC",
        "size": 1,
        "page": 0,
    }
    strUrl, dctParams, dctHeaders, strApiKey, strApiSecret = _build_trendyol_request(docSettings, dctParams)

    try:
        dctResponse = requests.get(
            strUrl,
            params=dctParams,
            auth=HTTPBasicAuth(strApiKey, strApiSecret),
            headers=dctHeaders,
            timeout=15,
        )
        _log_trendyol_call(docSettings, "Trendyol Check Connection", "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dctResponse.status_code, dctResponse.text)
    except requests.exceptions.RequestException as ex:
        _log_trendyol_call(docSettings, "Trendyol Check Connection", "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret)
        dctResponse = None

    if dctResponse is None:
        dctResult = frappe._dict({
            "op_result": False,
            "op_message": "Could not reach Trendyol API. Check network/firewall settings.",
        })
    elif dctResponse.status_code == 200:
        dctData = dctResponse.json()
        dctResult = frappe._dict({
            "op_result": True,
            "op_message": f"Connected. Account has {dctData.get('totalElements', 0)} total orders on file.",
        })
    elif dctResponse.status_code == 401:
        dctResult = frappe._dict({
            "op_result": False,
            "op_message": "Authentication failed — check API Key / Secret and environment (Production vs Staging).",
        })
    elif dctResponse.status_code == 403:
        dctResult = frappe._dict({
            "op_result": False,
            "op_message": "Blocked (403) — User-Agent header may be missing or rate limit exceeded.",
        })
    elif dctResponse.status_code == 429:
        dctResult = frappe._dict({
            "op_result": False,
            "op_message": "Rate limited by Trendyol (429) — too many requests, wait and retry.",
        })
    else:
        _log_trendyol_call(docSettings, "Trendyol Check Connection", "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dctResponse.status_code, dctResponse.text)
        dctResult = frappe._dict({
            "op_result": False,
            "op_message": f"Unexpected response from Trendyol (status {dctResponse.status_code}). See Error Log.",
        })

    return dctResult


@frappe.whitelist()
def poll_orders():
    """Enqueue a background job to fetch orders from Trendyol."""
    frappe.enqueue(
        "trendyol_erpnext.utils._run_poll_orders",
        queue="long",
        timeout=1400,
        job_id="trendyol_poll_orders",
    )
    return frappe._dict({"op_result": True, "op_message": "Order poll enqueued."})


def _run_poll_orders():
    """Process order polling for every enabled Trendyol Settings record."""
    lstSettings = frappe.get_all("Trendyol Settings", filters={"enabled": 1}, pluck="name")

    for strSettingsName in lstSettings:
        docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)
        try:
            _fetch_company_orders(docSettings)
        except Exception as ex:
            frappe.log_error(
                "Trendyol poll_orders — company failed",
                frappe.get_traceback(),
            )


def _fetch_company_orders(docSettings):
    """Fetch and upsert orders from Trendyol for a single company using 14-day windows."""
    strCompany = docSettings.company

    if not _acquire_poll_lock(strCompany):
        frappe.log_error(
            "Trendyol poll_orders — skipped (previous job still running)",
            f"Company: {strCompany}",
        )
        return 0, 0

    try:
        return _fetch_company_orders_inner(docSettings)
    finally:
        _release_poll_lock(strCompany)


def _fetch_company_orders_inner(docSettings):
    """Inner fetch logic — runs under the poll lock."""
    strCompany = docSettings.company

    dtStart = _resolve_start_date(docSettings)
    if dtStart is None:
        frappe.throw(
            "Trendyol poll_orders — both Sync From Date and Last Order Fetch Date are empty. "
            "Set Sync From Date in Trendyol Settings."
        )

    dtEnd = datetime.now(GMT3)

    dFetched = 0
    dFailed = 0
    dTotalPagesFetched = 0

    while dtStart < dtEnd:
        dtWindowEnd = min(dtStart + timedelta(days=MAX_WINDOW_DAYS), dtEnd)
        dWindowFetched, dWindowFailed, dWindowPages, dtWindowLastMod = _fetch_date_window(
            docSettings, dtStart, dtWindowEnd,
        )
        dFetched += dWindowFetched
        dFailed += dWindowFailed
        dTotalPagesFetched += dWindowPages

        if dtWindowLastMod is not None:
            _save_checkpoint(docSettings, dtWindowLastMod)
            dtStart = dtWindowLastMod
        else:
            _save_checkpoint(docSettings, dtWindowEnd)
            dtStart = dtWindowEnd

        if dTotalPagesFetched >= MAX_POLL_PAGES:
            break

    return dFetched, dFailed


def _resolve_start_date(docSettings):
    """Determine the sync start date from last_order_fetch_date or sync_from_date."""
    dtLastFetch = None
    if docSettings.last_order_fetch_date:
        try:
            dtLastFetch = datetime.fromisoformat(docSettings.last_order_fetch_date)
            if dtLastFetch.tzinfo is None:
                dtLastFetch = dtLastFetch.replace(tzinfo=GMT3)
        except (ValueError, TypeError):
            dtLastFetch = None

    dtSyncFrom = None
    if docSettings.sync_from_date:
        try:
            dtSyncFrom = datetime.combine(docSettings.sync_from_date, datetime.min.time()).replace(tzinfo=GMT3)
        except (ValueError, TypeError):
            dtSyncFrom = None

    dtResult = None
    if dtLastFetch is not None:
        dtResult = dtLastFetch
    if dtSyncFrom is not None and (dtResult is None or dtSyncFrom > dtResult):
        dtResult = dtSyncFrom

    return dtResult


def _fetch_date_window(docSettings, dtStart, dtEnd):
    """Fetch all pages within a single date window. Returns (fetched, failed, pagesProcessed, lastModDate)."""
    strCompany = docSettings.company
    dctParams = {
        "startDate": _to_epoch_ms(dtStart),
        "endDate": _to_epoch_ms(dtEnd),
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "ASC",
        "size": 50,
        "page": 0,
    }
    strUrl, dctParams, dctHeaders, strApiKey, strApiSecret = _build_trendyol_request(docSettings, dctParams)

    dPage = 0
    dTotalPages = 1
    dFetched = 0
    dFailed = 0
    dtLatestMod = None

    while dPage < dTotalPages and dPage < MAX_POLL_PAGES:
        dctParams["page"] = dPage

        dctResponse = _fetch_trendyol_page(
            docSettings, strUrl, dctHeaders, dctParams,
            strApiKey, strApiSecret,
        )

        if dctResponse is None or dctResponse.status_code != 200:
            dFailed += 1
            if dctResponse is not None:
                frappe.log_error(
                    "Trendyol poll_orders — page fetch failed",
                    f"Status: {dctResponse.status_code}, Page: {dPage}, Window: {dtStart.date()} to {dtEnd.date()}",
                )
            dPage += 1
            continue

        dctData = dctResponse.json()
        dTotalPages = dctData.get("totalPages", 0)
        lstContent = dctData.get("content", [])

        if not lstContent:
            break

        for dctOrderContent in lstContent:
            if docSettings.order_status != "ALL" and dctOrderContent.get("status", "") in TRENDYOL_STATUSES_TO_SKIP:
                continue
            try:
                docOrder = _upsert_order(dctOrderContent, strCompany)
                _upsert_payload(docOrder, dctOrderContent)
                dFetched += 1
            except Exception as ex:
                frappe.log_error(
                    "Trendyol poll_orders — order upsert failed",
                    frappe.get_traceback(),
                )
                dFailed += 1

            strLastModMs = dctOrderContent.get("lastModifiedDate")
            if strLastModMs:
                try:
                    dtMod = datetime.fromtimestamp(int(strLastModMs) / 1000, tz=GMT3)
                    if dtLatestMod is None or dtMod > dtLatestMod:
                        dtLatestMod = dtMod
                except (ValueError, TypeError, OSError):
                    pass

        dPage += 1

    if dtLatestMod is not None:
        _save_checkpoint(docSettings, dtLatestMod)

    return dFetched, dFailed, dPage, dtLatestMod


def _save_checkpoint(docSettings, dtModDate):
    """Save the last_order_fetch_date checkpoint."""
    try:
        docSettings.db_set("last_order_fetch_date", dtModDate.isoformat(), update_modified=False)
    except Exception:
        frappe.log_error(
            "Trendyol poll_orders — failed to update last_order_fetch_date",
            frappe.get_traceback(),
        )


def _fetch_trendyol_page(docSettings, strUrl, dctHeaders, dctParams, strApiKey, strApiSecret):
    """Fetch a single page from the Trendyol orders API, returning the response or None."""
    dctResponse = None
    try:
        dctResponse = requests.get(
            strUrl,
            params=dctParams,
            auth=HTTPBasicAuth(strApiKey, strApiSecret),
            headers=dctHeaders,
            timeout=30,
        )
        _log_trendyol_call(
            docSettings, "Trendyol Get Orders", "GET", strUrl, dctHeaders, dctParams,
            strApiKey, strApiSecret,
            dctResponse.status_code, dctResponse.text,
        )
    except requests.exceptions.RequestException:
        _log_trendyol_call(
            docSettings, "Trendyol Get Orders", "GET", strUrl, dctHeaders, dctParams,
            strApiKey, strApiSecret,
        )
        dctResponse = None

    return dctResponse


def _upsert_order(dctOrderContent, strCompany):
    """Create or update a Trendyol Order from an API order payload."""
    strOrderNumber = str(dctOrderContent.get("orderNumber", ""))

    strExistingName = frappe.db.exists("Trendyol Order", {"order_number": strOrderNumber})
    if strExistingName:
        docOrder = frappe.get_doc("Trendyol Order", strExistingName)
    else:
        docOrder = frappe.new_doc("Trendyol Order")

    if docOrder.status not in ("Completed", "Processing"):
        docOrder.order_number = strOrderNumber
        docOrder.shipment_package_id = str(dctOrderContent.get("shipmentPackageId", ""))
        docOrder.trendyol_status = dctOrderContent.get("status", "")
        docOrder.gross_amount = dctOrderContent.get("grossAmount")
        docOrder.total_price = dctOrderContent.get("totalPrice")
        docOrder.customer_name = f"{dctOrderContent.get('customerFirstName', '')} {dctOrderContent.get('customerLastName', '')}".strip()
        docOrder.customer_email = dctOrderContent.get("customerEmail", "")
        docOrder.currency = dctOrderContent.get("currencyCode", "")
        docOrder.cargo_tracking_number = str(dctOrderContent.get("cargoTrackingNumber", ""))
        docOrder.cargo_provider_name = dctOrderContent.get("cargoProviderName", "")
        docOrder.shipment_address = json.dumps(dctOrderContent.get("shipmentAddress"), ensure_ascii=False)
        docOrder.invoice_address = json.dumps(dctOrderContent.get("invoiceAddress"), ensure_ascii=False)
        docOrder.commercial = 1 if dctOrderContent.get("commercial") else 0
        docOrder.company = strCompany

        if not strExistingName:
            docOrder.status = "New"

        strOrderDateMs = dctOrderContent.get("orderDate")
        if strOrderDateMs:
            try:
                docOrder.order_date = datetime.fromtimestamp(int(strOrderDateMs) / 1000)
            except (ValueError, TypeError, OSError):
                pass

        docOrder.set("items", [])
        for dctLine in dctOrderContent.get("lines", []):
            docOrder.append("items", {
                "product_name": dctLine.get("productName", ""),
                "quantity": dctLine.get("quantity"),
                "price": dctLine.get("price"),
                "amount": dctLine.get("amount"),
                "vat_rate": dctLine.get("vatRate"),
                "discount_total": dctLine.get("lineTotalDiscount"),
                "barcode": dctLine.get("barcode", ""),
                "sku": dctLine.get("sku", ""),
                "stock_code": dctLine.get("stockCode", ""),
                "merchant_sku": dctLine.get("merchantSku", ""),
            })

        docOrder.save(ignore_permissions=True)

    return docOrder


def _upsert_payload(docOrder, dctOrderContent):
    """Archive the raw Trendyol API response for an order."""
    strExistingPayload = frappe.db.exists("Trendyol Order Payload", {"trendyol_order": docOrder.name})
    if strExistingPayload:
        docPayload = frappe.get_doc("Trendyol Order Payload", strExistingPayload)
    else:
        docPayload = frappe.new_doc("Trendyol Order Payload")
        docPayload.trendyol_order = docOrder.name
    docPayload.payload = dctOrderContent
    docPayload.fetched_at = datetime.now()
    docPayload.save(ignore_permissions=True)


@frappe.whitelist()
def process_staged_orders(limit=20):
	"""Enqueue staged Trendyol orders for background processing."""
	dLimit = 20 if limit is None else int(limit)
	if dLimit <= 0:
		lstOrders = []
	else:
		lstOrders = frappe.get_all(
			"Trendyol Order",
			filters={
				"status": ["in", ("New", "Failed")],
				"retry_count": ["<", 3],
			},
			order_by="creation asc",
			limit=min(dLimit, 20),
			pluck="name",
		)

	dctSettingsByCompany = {}
	for strSettingsName in frappe.get_all("Trendyol Settings", filters={"enabled": 1}, pluck="name"):
		docS = frappe.get_doc("Trendyol Settings", strSettingsName)
		dctSettingsByCompany[docS.company] = docS

	dEnqueued = 0
	dSkipped = 0

	for strOrderName in lstOrders:
		docOrder = frappe.get_doc("Trendyol Order", strOrderName)
		docSettings = dctSettingsByCompany.get(docOrder.company)
		if not docSettings:
			frappe.log_error(
				"Trendyol process_staged_orders — no settings for company",
				f"Order {strOrderName} company={docOrder.company}",
			)
			dSkipped += 1
			continue

		try:
			frappe.enqueue(
				"trendyol_erpnext.utils._process_single_order_job",
				queue="long",
				timeout=600,
				order_name=strOrderName,
				settings_name=docSettings.name,
				job_id=f"trendyol_process_{strOrderName}",
			)
			dEnqueued += 1
		except Exception:
			frappe.log_error(
				"Trendyol process_staged_orders — enqueue failed",
				frappe.get_traceback(),
			)
			dSkipped += 1

	dctResult = frappe._dict({
		"op_result": True,
		"op_message": f"Enqueued {dEnqueued}, skipped {dSkipped} of {len(lstOrders)} orders.",
		"enqueued": dEnqueued,
		"skipped": dSkipped,
		"total": len(lstOrders),
	})
	return dctResult


def _process_single_order_job(order_name, settings_name):
	"""Process one staged Trendyol order in a background job."""
	try:
		docOrder = frappe.get_doc("Trendyol Order", order_name)
		docSettings = frappe.get_doc("Trendyol Settings", settings_name)
		_process_single_order(docOrder, docSettings)
	except Exception:
		frappe.log_error(
			f"Trendyol process job failed for {order_name}",
			frappe.get_traceback(),
		)


@frappe.whitelist()
def create_sales_order_from_trendyol_order(strOrderName):
	"""Create a Sales Order from a single Trendyol Order. Called from UI button."""
	docOrder = frappe.get_doc("Trendyol Order", strOrderName)

	if docOrder.status in ("Completed", "Processing"):
		dctResult = frappe._dict({
			"op_result": False,
			"op_message": f"Order {docOrder.name} is already {docOrder.status}",
		})
		return dctResult

	strSettingsName = frappe.db.get_value("Trendyol Settings", {"company": docOrder.company}, "name")
	if not strSettingsName:
		dctResult = frappe._dict({
			"op_result": False,
			"op_message": f"No Trendyol Settings found for company {docOrder.company}",
		})
		return dctResult

	docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)
	try:
		_process_single_order(docOrder, docSettings)
	except Exception:
		frappe.log_error(
			f"Trendyol create_sales_order failed for {docOrder.name}",
			frappe.get_traceback(),
		)
		dctResult = frappe._dict({
			"op_result": False,
			"op_message": f"Failed to create Sales Order for {docOrder.name}. See Error Log.",
		})
		return dctResult

	dctResult = frappe._dict({
		"op_result": True,
		"op_message": f"Sales Order created for {docOrder.name}",
	})
	return dctResult


def _ensure_item(dctItem, docSettings, dctProductCodeMap):
    """Resolve an ERPNext Item for a Trendyol order line using the configured matching mode.

    dctProductCodeMap maps barcode → productCode from the stored payload.
    """
    strMatchMode = docSettings.item_matching or "Use Product Code"
    strItemCode = None

    if strMatchMode == "Use Item Map Table":
        strItemCode = _resolve_item_via_map(dctItem, docSettings, dctProductCodeMap)
    elif strMatchMode == "Match by Barcode":
        strItemCode = _resolve_item_via_barcode(dctItem)
    else:
        strItemCode = _resolve_item_via_product_code(dctItem, docSettings, dctProductCodeMap)

    if not strItemCode:
        if strMatchMode == "Match by Barcode":
            frappe.throw(
                f"No ERPNext Item found with barcode '{dctItem.barcode}' "
                f"for '{dctItem.product_name}'. "
                f"Add this barcode to an existing Item's Barcode table, "
                f"or create a new Item with this barcode."
            )
        else:
            frappe.throw(
                f"Item matching failed for '{dctItem.product_name}' "
                f"(barcode={dctItem.barcode}, mode={strMatchMode}). "
                f"Check Trendyol Settings configuration."
            )

    return strItemCode


def _resolve_item_via_product_code(dctItem, docSettings, dctProductCodeMap):
    """Match by stock_code → productCode → barcode."""
    if dctItem.stock_code and frappe.db.exists("Item", dctItem.stock_code):
        strItemCode = dctItem.stock_code
    else:
        strProductCode = dctProductCodeMap.get(dctItem.barcode or dctItem.sku, "")
        if strProductCode and frappe.db.exists("Item", strProductCode):
            strItemCode = strProductCode
        else:
            strItemCode = _resolve_item_via_barcode(dctItem)
    return strItemCode


def _resolve_item_via_barcode(dctItem):
    """Match by barcode: search Item Barcode table, prefer enabled items."""
    strBarcode = dctItem.barcode or dctItem.sku
    lstCandidates = []
    if strBarcode:
        lstCandidates = frappe.get_all(
            "Item Barcode",
            filters={"barcode": strBarcode},
            fields=["parent"],
            pluck="parent",
        )

    strItemCode = None
    if lstCandidates:
        strItemCode = lstCandidates[0]
        for strCandidate in lstCandidates:
            blnDisabled = frappe.db.get_value("Item", strCandidate, "disabled")
            if not blnDisabled:
                strItemCode = strCandidate
                break

    return strItemCode


def _resolve_item_via_map(dctItem, docSettings, dctProductCodeMap):
    """Match via Trendyol Item Map child table: productCode → Item Code."""
    strBarcode = dctItem.barcode or dctItem.sku
    strProductCode = dctProductCodeMap.get(strBarcode, "")
    strItemCode = None
    if strProductCode and docSettings.item_mapping:
        for dctRow in docSettings.item_mapping:
            if dctRow.code == strProductCode:
                strItemCode = dctRow.item_code
                break
    return strItemCode


def _build_product_code_map(docOrder):
    """Build a barcode→productCode map from the stored payload for an order."""
    dctMap = {}
    strPayloadName = frappe.db.get_value(
        "Trendyol Order Payload",
        {"trendyol_order": docOrder.name},
        "name",
    )
    if strPayloadName:
        docPayload = frappe.get_doc("Trendyol Order Payload", strPayloadName)
        dctPayload = docPayload.payload
        if isinstance(dctPayload, str):
            try:
                dctPayload = json.loads(dctPayload)
            except (json.JSONDecodeError, TypeError):
                dctPayload = {}

        if dctPayload:
            for dctLine in dctPayload.get("lines", []):
                strBarcode = dctLine.get("barcode", "")
                strProductCode = frappe.utils.cstr(dctLine.get("productCode", ""))
                if strBarcode and strProductCode:
                    dctMap[strBarcode] = strProductCode

    return dctMap


def _extract_customer_id(docOrder):
    """Extract Trendyol customerId from the stored payload for an order."""
    strCustId = ""
    strPayloadName = frappe.db.get_value(
        "Trendyol Order Payload",
        {"trendyol_order": docOrder.name},
        "name",
    )
    if strPayloadName:
        docPayload = frappe.get_doc("Trendyol Order Payload", strPayloadName)
        dctPayload = docPayload.payload
        if isinstance(dctPayload, str):
            try:
                dctPayload = json.loads(dctPayload)
            except (json.JSONDecodeError, TypeError):
                dctPayload = {}
        if dctPayload:
            strCustId = frappe.utils.cstr(dctPayload.get("customerId", ""))
    return strCustId


def _ensure_shipping_address(docOrder, docSettings, strCustomer, strTrendyolCustId, strAddressType="Shipping"):
    """Create or update an Address from Trendyol data.

    Address name = "{firstName} {lastName} {trendyolCustomerId}" (Shipping)
    or "{firstName} {lastName} {trendyolCustomerId} - Billing" (Billing).
    If address already exists and fields changed, update it.
    If not found, create it.
    """
    if strAddressType == "Billing":
        dctAddrData = _parse_address_json(docOrder.invoice_address)
    else:
        dctAddrData = _parse_address_json(docOrder.shipment_address)
    strFirstName = dctAddrData.get("firstName", "")
    strLastName = dctAddrData.get("lastName", "")

    if strFirstName or strLastName:
        strSuffix = f" - {strAddressType}" if strAddressType != "Shipping" else ""
        strAddressName = f"{strFirstName} {strLastName} {strTrendyolCustId}{strSuffix}".strip()
        blnExists = frappe.db.exists("Address", strAddressName)
        if blnExists:
            _update_address_if_changed(strAddressName, dctAddrData, strCustomer)
        else:
            _create_shipping_address(strAddressName, dctAddrData, strCustomer, strAddressType)
        strResult = strAddressName
    else:
        strResult = _fallback_shipping_address(strCustomer)

    return strResult


def _parse_address_json(strRaw):
    """Parse an address JSON string or dict from Trendyol, returning a dict."""
    dctResult = {}
    if strRaw:
        if isinstance(strRaw, dict):
            dctResult = strRaw
        else:
            try:
                dctResult = json.loads(strRaw)
            except (json.JSONDecodeError, TypeError):
                pass
    return dctResult


def _create_shipping_address(strAddressName, dctAddrData, strCustomer, strAddressType="Shipping"):
    """Create a new Address and link it to the customer."""
    docAddress = frappe.get_doc({
        "doctype": "Address",
        "address_type": strAddressType,
        "address_line1": dctAddrData.get("address1", ""),
        "address_line2": dctAddrData.get("address2", ""),
        "city": dctAddrData.get("city", ""),
        "county": dctAddrData.get("district", ""),
        "state": dctAddrData.get("stateName", ""),
        "pincode": dctAddrData.get("postalCode", ""),
        "country": _resolve_country(dctAddrData.get("countryCode", "TR")),
        "phone": dctAddrData.get("phone"),
        "links": [{"link_doctype": "Customer", "link_name": strCustomer}],
    })
    docAddress.insert(ignore_permissions=True)
    frappe.rename_doc("Address", docAddress.name, strAddressName, force=True)


def _update_address_if_changed(strAddressName, dctAddrData, strCustomer):
    """Update an existing Address if any field changed, and ensure the customer link exists."""
    docAddress = frappe.get_doc("Address", strAddressName)

    dctFieldMap = {
        "address_line1": dctAddrData.get("address1", ""),
        "address_line2": dctAddrData.get("address2", ""),
        "city": dctAddrData.get("city", ""),
        "county": dctAddrData.get("district", ""),
        "state": dctAddrData.get("stateName", ""),
        "pincode": dctAddrData.get("postalCode", ""),
        "country": _resolve_country(dctAddrData.get("countryCode", "TR")),
        "phone": dctAddrData.get("phone"),
    }

    blnChanged = False
    for strField, strNewVal in dctFieldMap.items():
        strCurrentVal = docAddress.get(strField) or ""
        if str(strCurrentVal).strip() != str(strNewVal).strip():
            docAddress.set(strField, strNewVal)
            blnChanged = True

    blnLinked = any(
        d.link_doctype == "Customer" and d.link_name == strCustomer
        for d in docAddress.links
    )
    if not blnLinked:
        docAddress.append("links", {"link_doctype": "Customer", "link_name": strCustomer})
        blnChanged = True

    if blnChanged:
        docAddress.save(ignore_permissions=True)


def _resolve_country(strCountryCode):
    """Resolve a 2-letter country code to the full Country name used in ERPNext."""
    strFullName = ""
    if strCountryCode:
        strFullName = frappe.db.get_value("Country", {"code": strCountryCode.lower()}, "name")
    if not strFullName:
        strFullName = "Turkey"
    return strFullName


def _fallback_shipping_address(strCustomer):
    """Return the first Shipping Address linked to the customer, or empty string."""
    lstExisting = frappe.get_list(
        "Address",
        filters={"link_name": strCustomer, "address_type": "Shipping"},
        limit=1,
        pluck="name",
    )
    return lstExisting[0] if lstExisting else ""


def _create_commercial_customer(docOrder, docSettings, strTrendyolCustId):
    """Create a Customer from Trendyol order details for commercial orders.

    Address is created first (without a customer link), then the Customer is
    created with ``customer_primary_address`` pointing to it, and finally the
    Address is back-linked to the Customer.  This avoids the circular
    dependency and eliminates the need for ``ignore_mandatory=True``.
    """
    dctInvAddr = _parse_address_json(docOrder.invoice_address)
    strCompanyName = dctInvAddr.get("company", "").strip() or docOrder.customer_name

    # --- Step 1: create Billing Address (no customer link yet) --------------
    docBillingAddr = frappe.get_doc({
        "doctype": "Address",
        "address_title": f"{strCompanyName} {strTrendyolCustId}",
        "address_type": "Billing",
        "address_line1": dctInvAddr.get("address1", ""),
        "address_line2": dctInvAddr.get("address2", ""),
        "city": dctInvAddr.get("city", ""),
        "county": dctInvAddr.get("district", ""),
        "state": dctInvAddr.get("stateName", ""),
        "pincode": dctInvAddr.get("postalCode", ""),
        "country": _resolve_country(dctInvAddr.get("countryCode", "TR")),
        "phone": dctInvAddr.get("phone"),
    })
    docBillingAddr.insert()
    strBillingAddrName = docBillingAddr.name

    # --- Step 2: create Customer with all mandatory fields ------------------
    strDefaultCurrency = frappe.db.get_value("Company", docSettings.company, "default_currency") or "TRY"
    docCustomer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": strCompanyName,
        "customer_group": docSettings.default_customer_group or "All Customer Groups",
        "territory": docSettings.default_territory or "All Territories",
        "customer_type": "Company",
        "trendyol_customer_id": strTrendyolCustId,
        "default_currency": strDefaultCurrency,
        "customer_primary_address": strBillingAddrName,
        "language": "tr",
        "tax_id": dctInvAddr.get("taxNumber") or "-",
    })
    if docOrder.customer_email:
        docCustomer.email_id = docOrder.customer_email
    if dctInvAddr.get("taxOffice"):
        docCustomer.custom_tax_office = dctInvAddr.get("taxOffice")
    docCustomer.insert()

    # --- Step 3: back-link Address to Customer ------------------------------
    docBillingAddr = frappe.get_doc("Address", strBillingAddrName)
    docBillingAddr.append("links", {"link_doctype": "Customer", "link_name": docCustomer.name})
    docBillingAddr.save()

    return docCustomer.name


def _resolve_customer(docOrder, docSettings, strTrendyolCustId):
    """Resolve customer: commercial orders get a new Customer, others use default."""
    if docOrder.commercial:
        strExisting = frappe.db.get_value("Customer", {"trendyol_customer_id": strTrendyolCustId}, "name")
        if not strExisting:
            strExisting = _create_commercial_customer(docOrder, docSettings, strTrendyolCustId)
        strResult = strExisting
    else:
        strResult = docSettings.default_customer or "Guest"
    return strResult


def _process_single_order(docOrder, docSettings):
    """Convert a single staged Trendyol Order into an ERPNext Sales Order."""
    docOrder.status = "Processing"
    docOrder.save(ignore_permissions=True)

    try:
        dctProductCodeMap = _build_product_code_map(docOrder)
        strTrendyolCustId = _extract_customer_id(docOrder)
        strCustomer = _resolve_customer(docOrder, docSettings, strTrendyolCustId)

        docSO = frappe.new_doc("Sales Order")
        docSO.customer = strCustomer
        docSO.company = docSettings.company
        dtOrderDate = docOrder.order_date or datetime.now().date()
        docSO.transaction_date = dtOrderDate
        docSO.delivery_date = dtOrderDate + timedelta(days=3)
        docSO.order_type = "Sales"
        docSO.currency = "TRY"
        docSO.selling_price_list = "Standart Satış"
        docSO.shipping_address_name = _ensure_shipping_address(docOrder, docSettings, strCustomer, strTrendyolCustId)
        if docOrder.commercial:
            docSO.customer_address = frappe.db.get_value("Customer", strCustomer, "customer_primary_address")
        if docSettings.default_territory:
            docSO.territory = docSettings.default_territory
        strSalesPerson = docSettings.default_sales_person or "Satış Ekibi"
        docSO.append("sales_team", {"sales_person": strSalesPerson, "allocated_percentage": 100})

        lstVatRatesInOrder = set()
        for dctItem in docOrder.items:
            strItemCode = _ensure_item(dctItem, docSettings, dctProductCodeMap)
            docSO.append("items", {
                "item_code": strItemCode,
                "item_name": dctItem.product_name,
                "description": dctItem.product_name,
                "qty": dctItem.quantity,
                "rate": flt(dctItem.price),
                "warehouse": docSettings.default_warehouse,
                "delivery_date": docSO.delivery_date,
            })
            if flt(dctItem.vat_rate or 0):
                lstVatRatesInOrder.add(flt(dctItem.vat_rate))

        if docSettings.tax_mapping:
            for dctTaxMap in docSettings.tax_mapping:
                if flt(dctTaxMap.vat_rate) in lstVatRatesInOrder:
                    docSO.append("taxes", {
                        "charge_type": "On Net Total",
                        "account_head": dctTaxMap.account_head,
                        "rate": dctTaxMap.vat_rate,
                        "description": f"KDV %{int(dctTaxMap.vat_rate)}",
                        "included_in_print_rate": 1,
                    })

        docSO.insert(ignore_permissions=True)

        # --- Round monetary values to avoid float drift on submit -----------
        dPrecision = cint(frappe.db.get_default("currency_precision")) or 4
        for dctItemRow in docSO.items:
            dctItemRow.amount = rounded(dctItemRow.amount, dPrecision)
            dctItemRow.net_amount = rounded(dctItemRow.net_amount, dPrecision)
            dctItemRow.discount_amount = rounded(dctItemRow.discount_amount, dPrecision)
            dctItemRow.rate = rounded(dctItemRow.rate, dPrecision)
        docSO.save(ignore_permissions=True)
        docSO.submit()

        docOrder.status = "Completed"
        docOrder.sales_order = docSO.name
        docOrder.processed_at = datetime.now()
        docOrder.error_message = ""
        docOrder.error_type = ""
        docOrder.save(ignore_permissions=True)

    except Exception as ex:
        docOrder.status = "Failed"
        docOrder.retry_count = (docOrder.retry_count or 0) + 1
        docOrder.error_type = type(ex).__name__
        docOrder.error_message = frappe.get_traceback()
        docOrder.save(ignore_permissions=True)
        raise


# Prefix used by the LOGO Tiger Integration app for e-invoice PDF attachments
# (tiger_integration: pdf_filename = f"ELOGO_SALES_INVOICE_{doc_name}.pdf").
STR_INVOICE_FILE_PREFIX = "ELOGO_SALES_INVOICE"


def _send_invoice_pdf_to_trendyol(docOrder, docSettings, strFilePrefix=None, dctResult=None):
	"""Validate the chain and upload the invoice PDF for one Trendyol Order.

	Starts from already-loaded Trendyol Order and Settings documents. When
	``dctResult`` is supplied its steps/result are extended in place (used by the
	manual wrappers that prepend their own load steps). Returns frappe._dict with
	op_result, op_message, steps and already_uploaded. Single return at the end.
	"""
	if dctResult is None:
		dctResult = frappe._dict({"op_result": False, "op_message": "", "steps": [], "already_uploaded": False})

	def add_step(strStep, strStatus, strMessage=""):
		dctResult.steps.append({"step": strStep, "status": strStatus, "message": strMessage})

	blnProceed = True

	# Step 1: shipment_package_id
	add_step("Step 1: Shipment Package ID", "info", "Verifying shipment_package_id...")
	if blnProceed and not docOrder.shipment_package_id:
		add_step("Step 1: Shipment Package ID", "error", "shipment_package_id is empty on Trendyol Order")
		dctResult.op_message = "shipment_package_id is empty on Trendyol Order"
		blnProceed = False
	elif blnProceed:
		add_step("Step 1: Shipment Package ID", "success", f"shipment_package_id: {docOrder.shipment_package_id}")

	# Step 2: Settings credentials
	add_step("Step 2: Settings", "info", "Loading Trendyol credentials...")
	strApiKey = docSettings.get_password("api_key")
	strApiSecret = docSettings.get_password("api_secret")
	if blnProceed and (not strApiKey or not strApiSecret):
		add_step("Step 2: Settings", "error", "API Key or API Secret not configured")
		dctResult.op_message = "API Key or API Secret not configured"
		blnProceed = False
	elif blnProceed:
		add_step("Step 2: Settings", "success", f"Supplier ID: {docSettings.supplier_id}")

	# Step 3: submitted Sales Invoice (linked via Sales Invoice Item child table)
	add_step("Step 3: Sales Invoice", "info", "Searching for submitted Sales Invoice linked to this SO...")
	strSIName = None
	if blnProceed:
		lstSIRows = frappe.get_all(
			"Sales Invoice Item",
			filters={"sales_order": docOrder.sales_order},
			fields=["parent"],
			distinct=True,
		)
		for dctSIRow in lstSIRows:
			if frappe.db.get_value("Sales Invoice", dctSIRow.parent, "docstatus") == 1:
				strSIName = dctSIRow.parent
				break
	if blnProceed and not strSIName:
		add_step("Step 3: Sales Invoice", "error", "No submitted Sales Invoice found for this Sales Order")
		dctResult.op_message = "No submitted Sales Invoice found for this Sales Order"
		blnProceed = False
	elif blnProceed:
		add_step("Step 3: Sales Invoice", "success", f"Found: {strSIName}")

	# Step 4: PDF attachment
	add_step("Step 4: PDF Attachment", "info", "Searching for attached e-invoice PDF...")
	strFileName = None
	lstPDFFiles = []
	if blnProceed:
		dctFileFilter = {
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": strSIName,
		}
		dctFileFilter["file_name"] = ["like", f"{strFilePrefix}%"] if strFilePrefix else ["like", "%.pdf"]
		lstPDFFiles = frappe.get_all(
			"File",
			filters=dctFileFilter,
			fields=["name", "file_name"],
			order_by="creation desc",
			limit=1,
		)
		if lstPDFFiles:
			strFileName = lstPDFFiles[0].file_name
	if blnProceed and not strFileName:
		strMsg = "No PDF file attached" + (f" starting with {strFilePrefix}" if strFilePrefix else "")
		add_step("Step 4: PDF Attachment", "error", strMsg)
		dctResult.op_message = "No matching PDF file attached to this Sales Invoice"
		blnProceed = False
	elif blnProceed:
		add_step("Step 4: PDF Attachment", "success", f"Found PDF: {strFileName}")

	# Step 5: Read PDF bytes
	bytesPDFContent = None
	if blnProceed:
		add_step("Step 5: Read PDF", "info", "Reading PDF file content...")
		try:
			bytesPDFContent = frappe.get_doc("File", lstPDFFiles[0].name).get_content()
		except Exception:
			bytesPDFContent = None
		if not bytesPDFContent or len(bytesPDFContent) < 100:
			add_step("Step 5: Read PDF", "error", f"PDF content empty or too small ({len(bytesPDFContent or b'')} bytes)")
			dctResult.op_message = "PDF content is empty or too small"
			blnProceed = False
		else:
			add_step("Step 5: Read PDF", "success", "PDF read successfully")

	# Step 6: Upload to Trendyol
	if blnProceed:
		add_step("Step 6: Upload to Trendyol", "info", "Uploading PDF to Trendyol API...")
		strUploadUrl = f"{docSettings.service_url.rstrip('/')}/integration/sellers/{docSettings.supplier_id}/seller-invoice-file"
		dctHeaders = {
			"User-Agent": f"{docSettings.supplier_id} - SelfIntegration",
			"storeFrontCode": "TR",
			"Accept": "*/*",
		}
		dctFiles = {
			"shipmentPackageId": (None, str(docOrder.shipment_package_id)),
			"file": (strFileName, bytesPDFContent, "application/pdf"),
		}
		try:
			dctResponse = requests.post(
				strUploadUrl,
				auth=HTTPBasicAuth(strApiKey, strApiSecret),
				headers=dctHeaders,
				files=dctFiles,
				timeout=30,
			)
		except requests.exceptions.RequestException:
			add_step("Step 6: Upload to Trendyol", "error", f"Network error: {frappe.get_traceback()}")
			dctResult.op_message = "Network error while uploading to Trendyol"
			dctResponse = None

		if dctResponse is None:
			pass
		elif dctResponse.status_code in (200, 201):
			add_step("Step 6: Upload to Trendyol", "success", f"Upload successful (HTTP {dctResponse.status_code})")
			dctResult.op_result = True
			dctResult.op_message = f"Invoice PDF uploaded successfully for {strSIName}"
		elif dctResponse.status_code == 401:
			add_step("Step 6: Upload to Trendyol", "error", "Authentication failed (401) - check API Key / Secret")
			dctResult.op_message = "Authentication failed - check API Key / Secret"
		elif dctResponse.status_code == 403:
			add_step("Step 6: Upload to Trendyol", "error", "Blocked (403) - check User-Agent header or storeFrontCode")
			dctResult.op_message = "Blocked by Trendyol (403) - check User-Agent or storeFrontCode"
		elif dctResponse.status_code == 429:
			add_step("Step 6: Upload to Trendyol", "error", "Rate limited (429) - too many requests, wait and retry")
			dctResult.op_message = "Rate limited by Trendyol (429)"
		elif dctResponse.status_code == 400:
			strRespBody = dctResponse.text[:2000] if dctResponse.text else ""
			dctRespJSON = {}
			try:
				dctRespJSON = dctResponse.json()
			except Exception:
				pass
			lstErrors = dctRespJSON.get("errors", [])
			strTrendyolMsg = lstErrors[0].get("message", "") if lstErrors else strRespBody
			if "önceden gönderilmiş" in strTrendyolMsg or "already" in strTrendyolMsg.lower():
				add_step("Step 6: Upload to Trendyol", "error", f"Invoice already uploaded for this package: {strTrendyolMsg}")
				dctResult.op_message = "Invoice already uploaded for this shipment package"
				dctResult.already_uploaded = True
			else:
				add_step("Step 6: Upload to Trendyol", "error", f"HTTP 400: {strTrendyolMsg}")
				dctResult.op_message = f"Bad request from Trendyol: {strTrendyolMsg}"
			frappe.log_error(
				"Trendyol invoice upload - upload failed",
				f"Status: {dctResponse.status_code}\nBody: {strRespBody}",
			)
		else:
			strRespBody = dctResponse.text[:2000] if dctResponse.text else ""
			add_step("Step 6: Upload to Trendyol", "error", f"HTTP {dctResponse.status_code}: {strRespBody}")
			dctResult.op_message = f"Unexpected response from Trendyol (HTTP {dctResponse.status_code})"
			frappe.log_error(
				"Trendyol invoice upload - upload failed",
				f"Status: {dctResponse.status_code}\nBody: {strRespBody}",
			)

	return dctResult


def _build_invoice_comment(dctResult, strTitle=None):
	"""Build the step-by-step HTML comment text for an invoice operation result.

	``strTitle`` defaults to the upload title; pass a custom string for deletes.
	"""
	if not strTitle:
		strTitle = "Trendyol Invoice PDF Upload Results"
	strCommentText = f"{strTitle}\n"
	for dctStep in dctResult.steps:
		strIcon = "OK" if dctStep["status"] == "success" else ("ERR" if dctStep["status"] == "error" else "INFO")
		strCommentText += f"<br>{strIcon} {dctStep['step']}: {dctStep['message']}"
	strCommentText += f"<br><br>Final Result: {'SUCCESS' if dctResult.op_result else 'FAILED'}<br>"
	strCommentText += f"Message: {dctResult.op_message}"
	return strCommentText


def _finalize_invoice_upload(docOrder, dctResult):
	"""Attach the step log as a comment and mark the order sent when successful."""
	strCommentText = _build_invoice_comment(dctResult)
	try:
		docOrder.add_comment(
			"Comment",
			text=strCommentText,
			comment_email=frappe.session.user,
			comment_by=frappe.session.user,
		)
	except Exception:
		frappe.log_error("Trendyol invoice upload - comment failed", frappe.get_traceback())

	if dctResult.op_result or getattr(dctResult, "already_uploaded", False):
		docOrder.invoice_sent = 1
		docOrder.invoice_processed_at = frappe.utils.now_datetime()
		docOrder.save(ignore_permissions=True)


@frappe.whitelist()
def test_send_invoice_pdf(strSalesOrderName):
	"""Validate chain (via Sales Order) and upload invoice PDF to Trendyol."""
	dctResult = frappe._dict({"op_result": False, "op_message": "", "steps": [], "already_uploaded": False})

	def add_step(strStep, strStatus, strMessage=""):
		dctResult.steps.append({"step": strStep, "status": strStatus, "message": strMessage})

	add_step("Step 0: Load Sales Order", "info", f"Fetching {strSalesOrderName}...")
	if not frappe.db.exists("Sales Order", strSalesOrderName):
		add_step("Step 0: Load Sales Order", "error", f"Sales Order {strSalesOrderName} not found")
		dctResult.op_message = f"Sales Order {strSalesOrderName} not found"
		return dctResult
	add_step("Step 0: Load Sales Order", "success", f"Sales Order {strSalesOrderName} loaded")

	strTrendyolOrderName = frappe.db.get_value("Trendyol Order", {"sales_order": strSalesOrderName}, "name")
	if not strTrendyolOrderName:
		add_step("Step 0: Trendyol Order", "error", "No Trendyol Order linked to this Sales Order")
		dctResult.op_message = "No Trendyol Order linked to this Sales Order"
		return dctResult
	docOrder = frappe.get_doc("Trendyol Order", strTrendyolOrderName)
	add_step("Step 0: Trendyol Order", "success", f"Found: {strTrendyolOrderName} (order #{docOrder.order_number})")

	strSettingsName = frappe.db.get_value("Trendyol Settings", {"company": docOrder.company}, "name")
	if not strSettingsName:
		add_step("Step 0: Settings", "error", f"No Trendyol Settings for company {docOrder.company}")
		dctResult.op_message = f"No Trendyol Settings for company {docOrder.company}"
		return dctResult
	docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)

	dctResult = _send_invoice_pdf_to_trendyol(
		docOrder, docSettings, strFilePrefix=STR_INVOICE_FILE_PREFIX, dctResult=dctResult
	)

	_finalize_invoice_upload(docOrder, dctResult)

	return dctResult


@frappe.whitelist()
def test_send_invoice_pdf_by_trendyol_order(strTrendyolOrderName):
	"""Validate chain (via Trendyol Order) and upload invoice PDF to Trendyol."""
	dctResult = frappe._dict({"op_result": False, "op_message": "", "steps": [], "already_uploaded": False})

	def add_step(strStep, strStatus, strMessage=""):
		dctResult.steps.append({"step": strStep, "status": strStatus, "message": strMessage})

	add_step("Step 0: Load Trendyol Order", "info", f"Fetching {strTrendyolOrderName}...")
	if not frappe.db.exists("Trendyol Order", strTrendyolOrderName):
		add_step("Step 0: Load Trendyol Order", "error", f"Trendyol Order {strTrendyolOrderName} not found")
		dctResult.op_message = f"Trendyol Order {strTrendyolOrderName} not found"
		return dctResult
	docOrder = frappe.get_doc("Trendyol Order", strTrendyolOrderName)
	add_step("Step 0: Load Trendyol Order", "success", f"Found: {strTrendyolOrderName} (order #{docOrder.order_number})")

	strSettingsName = frappe.db.get_value("Trendyol Settings", {"company": docOrder.company}, "name")
	if not strSettingsName:
		add_step("Step 0: Settings", "error", f"No Trendyol Settings for company {docOrder.company}")
		dctResult.op_message = f"No Trendyol Settings for company {docOrder.company}"
		return dctResult
	docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)

	dctResult = _send_invoice_pdf_to_trendyol(
		docOrder, docSettings, strFilePrefix=STR_INVOICE_FILE_PREFIX, dctResult=dctResult
	)

	_finalize_invoice_upload(docOrder, dctResult)

	return dctResult

@frappe.whitelist()
def process_invoice_uploads():
	"""Enqueue one background job per Trendyol Order that still needs an invoice upload.

	Enqueuing per order keeps each job tiny (a single ~30s network call) so it can
	never approach the worker timeout, and rq job_id dedup prevents duplicate
	uploads if the hourly cron fires while a job is still queued or running.
	"""
	lstOrderRows = frappe.get_all(
		"Trendyol Order",
		filters={"sales_order": ["!=", ""], "invoice_sent": 0},
		fields=["name", "company"],
		order_by="creation asc",
		limit=500,
	)
	if not lstOrderRows:
		return frappe._dict({"op_result": True, "op_message": "No pending invoice uploads."})

	lstEnabledCompanies = {
		frappe.db.get_value("Trendyol Settings", strName, "company")
		for strName in frappe.get_all("Trendyol Settings", filters={"enabled": 1}, pluck="name")
	}

	dCreated = 0
	for dctRow in lstOrderRows:
		if dctRow["company"] not in lstEnabledCompanies:
			continue
		frappe.enqueue(
			"trendyol_erpnext.utils._upload_one_invoice",
			queue="long",
			timeout=120,
			job_id=f"trendyol_invoice_upload_{dctRow['name']}",
			strOrderName=dctRow["name"],
		)
		dCreated += 1

	return frappe._dict({
		"op_result": True,
		"op_message": f"Enqueued {dCreated} invoice upload job(s).",
	})


def _upload_one_invoice(strOrderName):
	"""Upload the e-invoice PDF for a single Trendyol Order (background job)."""
	strCompany = frappe.db.get_value("Trendyol Order", strOrderName, "company")
	if not strCompany:
		return
	docOrder = frappe.get_doc("Trendyol Order", strOrderName)
	if docOrder.invoice_sent:
		return

	strSettingsName = frappe.db.get_value("Trendyol Settings", {"company": strCompany, "enabled": 1}, "name")
	if not strSettingsName:
		return
	docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)

	dctResult = _send_invoice_pdf_to_trendyol(docOrder, docSettings, STR_INVOICE_FILE_PREFIX)
	_finalize_invoice_upload(docOrder, dctResult)


@frappe.whitelist()
def delete_trendyol_invoice(strTrendyolOrderName):
	"""Delete the previously uploaded invoice PDF from Trendyol for one order.

	Returns frappe._dict with op_result, op_message and steps. The local
	``invoice_sent`` flag is reset only after Trendyol confirms deletion or
	returns 400 with a "not found / already deleted" message.
	"""
	dctResult = frappe._dict({"op_result": False, "op_message": "", "steps": []})

	def add_step(strStep, strStatus, strMessage=""):
		dctResult.steps.append({"step": strStep, "status": strStatus, "message": strMessage})

	# Step 0: load Trendyol Order
	add_step("Step 0: Load Trendyol Order", "info", f"Fetching {strTrendyolOrderName}...")
	if not frappe.db.exists("Trendyol Order", strTrendyolOrderName):
		add_step("Step 0: Load Trendyol Order", "error", "Trendyol Order not found")
		dctResult.op_message = "Trendyol Order not found"
		return dctResult
	docOrder = frappe.get_doc("Trendyol Order", strTrendyolOrderName)
	add_step("Step 0: Load Trendyol Order", "success", f"Found: {docOrder.name} (#{docOrder.order_number})")

	# Step 1: shipment_package_id
	add_step("Step 1: Shipment Package ID", "info", "Verifying shipment_package_id...")
	if not docOrder.shipment_package_id:
		add_step("Step 1: Shipment Package ID", "error", "shipment_package_id is empty")
		dctResult.op_message = "shipment_package_id is empty on Trendyol Order"
		return dctResult
	add_step("Step 1: Shipment Package ID", "success", f"shipment_package_id: {docOrder.shipment_package_id}")

	# Step 2: Settings credentials
	add_step("Step 2: Settings", "info", "Loading Trendyol credentials...")
	strSettingsName = frappe.db.get_value("Trendyol Settings", {"company": docOrder.company, "enabled": 1}, "name")
	if not strSettingsName:
		add_step("Step 2: Settings", "error", f"No enabled Trendyol Settings for company {docOrder.company}")
		dctResult.op_message = f"No enabled Trendyol Settings for company {docOrder.company}"
		return dctResult
	docSettings = frappe.get_doc("Trendyol Settings", strSettingsName)
	strApiKey = docSettings.get_password("api_key")
	strApiSecret = docSettings.get_password("api_secret")
	if not strApiKey or not strApiSecret:
		add_step("Step 2: Settings", "error", "API Key or API Secret not configured")
		dctResult.op_message = "API Key or API Secret not configured"
		return dctResult
	add_step("Step 2: Settings", "success", f"Supplier ID: {docSettings.supplier_id}")

	# Step 3: customerId from Trendyol Order Payload
	add_step("Step 3: Customer ID", "info", "Resolving Trendyol customerId from payload...")
	strCustomerId = _extract_customer_id(docOrder)
	if not strCustomerId:
		add_step("Step 3: Customer ID", "error", "Could not resolve customerId from Trendyol Order Payload")
		dctResult.op_message = "Could not resolve customerId for this order"
		return dctResult
	add_step("Step 3: Customer ID", "success", f"customerId: {strCustomerId}")

	# Step 4: Call Trendyol delete endpoint
	add_step("Step 4: Delete from Trendyol", "info", "Calling seller-invoice-links/delete...")
	strDeleteUrl = f"{docSettings.service_url.rstrip('/')}/integration/sellers/{docSettings.supplier_id}/seller-invoice-links/delete"
	dctDeleteHeaders = {
		"User-Agent": f"{docSettings.supplier_id} - SelfIntegration",
		"Content-Type": "application/json",
		"Accept": "*/*",
	}
	dctDeleteBody = {
		"serviceSourceId": int(docOrder.shipment_package_id),
		"channelId": 1,
		"customerId": int(strCustomerId),
	}
	try:
		dctResp = requests.post(
			strDeleteUrl,
			auth=HTTPBasicAuth(strApiKey, strApiSecret),
			headers=dctDeleteHeaders,
			json=dctDeleteBody,
			timeout=15,
		)
	except requests.exceptions.RequestException:
		add_step("Step 4: Delete from Trendyol", "error", f"Network error: {frappe.get_traceback()}")
		dctResult.op_message = "Network error while calling Trendyol delete endpoint"
		return dctResult

	strRespBody = dctResp.text[:2000] if dctResp.text else ""
	if dctResp.status_code in (200, 201):
		add_step("Step 4: Delete from Trendyol", "success", f"Invoice deleted (HTTP {dctResp.status_code})")
		dctResult.op_result = True
		dctResult.op_message = f"Invoice deleted successfully for order #{docOrder.order_number}"
	elif dctResp.status_code == 400:
		strLower = strRespBody.lower()
		if any(k in strLower for k in ("bulunamad", "not found", "bulunmaktadır", "already")):
			add_step("Step 4: Delete from Trendyol", "success", "Invoice already absent — treating as deleted")
			dctResult.op_result = True
			dctResult.op_message = f"Invoice was already absent for order #{docOrder.order_number}"
		else:
			add_step("Step 4: Delete from Trendyol", "error", f"HTTP 400: {strRespBody[:300]}")
			dctResult.op_message = f"Trendyol rejected delete: {strRespBody[:300]}"
			frappe.log_error("Trendyol delete invoice - 400", f"Body: {strRespBody}")
	elif dctResp.status_code == 401:
		add_step("Step 4: Delete from Trendyol", "error", "Authentication failed (401)")
		dctResult.op_message = "Authentication failed — check API Key / Secret"
	elif dctResp.status_code == 403:
		add_step("Step 4: Delete from Trendyol", "error", "Blocked (403) — check User-Agent or rate limit")
		dctResult.op_message = "Blocked by Trendyol (403)"
	elif dctResp.status_code == 429:
		add_step("Step 4: Delete from Trendyol", "error", "Rate limited (429)")
		dctResult.op_message = "Rate limited by Trendyol (429)"
	else:
		add_step("Step 4: Delete from Trendyol", "error", f"HTTP {dctResp.status_code}: {strRespBody[:300]}")
		dctResult.op_message = f"Unexpected response (HTTP {dctResp.status_code})"
		frappe.log_error("Trendyol delete invoice - unexpected", f"Status: {dctResp.status_code}\nBody: {strRespBody}")

	# Step 5: reset local flag on success
	if dctResult.op_result:
		add_step("Step 5: Local Update", "info", "Resetting invoice_sent flag...")
		docOrder.invoice_sent = 0
		docOrder.invoice_processed_at = None
		docOrder.save(ignore_permissions=True)
		add_step("Step 5: Local Update", "success", "invoice_sent reset to 0")
	else:
		add_step("Step 5: Local Update", "error", "Local flag NOT reset because delete failed")

	# Step 6: comment on Trendyol Order
	add_step("Step 6: Comment", "info", "Writing comment to Trendyol Order...")
	strCommentText = _build_invoice_comment(dctResult, f"Trendyol Invoice Delete — Order #{docOrder.order_number}")
	try:
		docOrder.add_comment(
			"Comment",
			text=strCommentText,
			comment_email=frappe.session.user,
			comment_by=frappe.session.user,
		)
		add_step("Step 6: Comment", "success", "Comment added to Trendyol Order")
	except Exception:
		add_step("Step 6: Comment", "error", f"Comment failed: {frappe.get_traceback()}")
		frappe.log_error("Trendyol delete invoice - comment failed", frappe.get_traceback())

	return dctResult


@frappe.whitelist()
def bulk_delete_invoice_from_trendyol(order_names):
	"""Delete invoice PDFs from Trendyol for multiple Trendyol Orders.

	Called from the Trendyol Order list view's \"Delete Sales Invoice PDF from
	Trendyol\" action. Requires a list of Trendyol Order document names. For
	each order, the single-order ``delete_trendyol_invoice`` is called, which
	handles the API call, local flag reset, and comment. Returns a summary
	frappe._dict with per-order status counts.
	"""
	import time

	if not order_names or (isinstance(order_names, list) and len(order_names) == 0):
		return frappe._dict({"op_result": False, "op_message": "No orders selected."})

	if not isinstance(order_names, list):
		order_names = [order_names]

	dSuccess = 0
	dFailed = 0
	dSkipped = 0
	lstFailedOrders = []

	for strName in order_names:
		if not frappe.db.exists("Trendyol Order", strName):
			dSkipped += 1
			continue

		docOrder = frappe.get_doc("Trendyol Order", strName)
		if not docOrder.shipment_package_id:
			frappe.get_doc("Trendyol Order", strName).add_comment(
				"Comment",
				text="Trendyol Invoice Delete — skipped: shipment_package_id is empty.",
				comment_email=frappe.session.user,
				comment_by=frappe.session.user,
			)
			dSkipped += 1
			continue

		if docOrder.invoice_sent == 0:
			dctResult = frappe._dict({"op_result": True, "op_message": "Already absent — no local flag to reset.", "steps": []})
			strCommentText = _build_invoice_comment(dctResult, f"Trendyol Invoice Delete — Order #{docOrder.order_number}")
			try:
				docOrder.add_comment("Comment", text=strCommentText, comment_email=frappe.session.user, comment_by=frappe.session.user)
			except Exception:
				pass
			dSkipped += 1
			continue

		dctResult = delete_trendyol_invoice(strName)
		if dctResult.op_result:
			dSuccess += 1
		else:
			dFailed += 1
			lstFailedOrders.append(f"{(docOrder.order_number or '<no number>')}: {dctResult.op_message}")

		time.sleep(0.25)

	# Build summary
	lstParts = []
	if dSuccess:
		lstParts.append(f"{dSuccess} deleted")
	if dFailed:
		lstParts.append(f"{dFailed} failed")
	if dSkipped:
		lstParts.append(f"{dSkipped} skipped")

	strMessage = ", ".join(lstParts)
	blnResult = dFailed == 0

	if blnResult:
		strMessage = f"Invoice delete complete: {strMessage}."
	else:
		strMessage = f"Invoice delete finished: {strMessage}."

	if lstFailedOrders:
		strMessage += f" Failures: {'; '.join(lstFailedOrders[:5])}"
		if len(lstFailedOrders) > 5:
			strMessage += f" (plus {len(lstFailedOrders) - 5} more)"

	return frappe._dict({"op_result": blnResult, "op_message": strMessage})

