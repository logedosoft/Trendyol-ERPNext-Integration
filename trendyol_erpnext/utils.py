import json
from datetime import datetime

import frappe
import requests
from requests.auth import HTTPBasicAuth

MAX_POLL_PAGES = 10


def _log_trendyol_call(docSettings, strMethod, strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dStatusCode=None, strResponseBody=None):
    """Log a Trendyol API call to the Error Log when logging is enabled."""
    if docSettings.enable_logging:
        strTitle = f"Trendyol API — {strMethod} {dStatusCode or 'ERR'} {strUrl}"
        lstParts = [
            f"Method: {strMethod}",
            f"URL: {strUrl}",
            f"API Key: {strApiKey}",
            f"API Secret: {strApiSecret}",
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
    strUrl = f"{strBaseUrl}/suppliers/{strSupplierId}/orders"
    dctHeaders = {
        "User-Agent": f"{strSupplierId} - SelfIntegration",
        "Accept": "*/*",
    }
    return strUrl, dctParams, dctHeaders, strApiKey, strApiSecret


@frappe.whitelist()
def check_connection(docname):
    """Verify connectivity and authentication against the Trendyol API."""
    docSettings = frappe.get_doc("Trendyol Settings", docname)
    dctParams = {
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "DESC",
        "size": 1,
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
        _log_trendyol_call(docSettings, "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dctResponse.status_code, dctResponse.text)
    except requests.exceptions.RequestException as ex:
        _log_trendyol_call(docSettings, "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret)
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
        _log_trendyol_call(docSettings, "GET", strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dctResponse.status_code, dctResponse.text)
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
        except Exception:
            frappe.log_error(
                "Trendyol poll_orders — company failed",
                frappe.get_traceback(),
            )


def _fetch_company_orders(docSettings):
    """Fetch and upsert orders from Trendyol for a single company."""
    strCompany = docSettings.company

    dctParams = {
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "DESC",
        "size": 50,
        "page": 0,
    }
    strUrl, dctParams, dctHeaders, strApiKey, strApiSecret = _build_trendyol_request(docSettings, dctParams)

    dtLastFetch = None
    if docSettings.last_order_fetch:
        try:
            dtLastFetch = datetime.fromisoformat(docSettings.last_order_fetch)
        except (ValueError, TypeError):
            dtLastFetch = None

    dPage = 0
    dTotalPages = 1
    dFetched = 0
    dFailed = 0
    blnStopped = False
    blnError = False

    while dPage < dTotalPages and dPage < MAX_POLL_PAGES:
        dctParams["page"] = dPage

        dctResponse = _fetch_trendyol_page(
            docSettings, strUrl, dctHeaders, dctParams,
            strApiKey, strApiSecret,
        )

        if dctResponse is None or dctResponse.status_code != 200:
            dFailed += 1
            blnError = True
            break

        dctData = dctResponse.json()
        dTotalPages = dctData.get("totalPages", 0)
        lstContent = dctData.get("content", [])

        if not lstContent:
            break

        blnAllOld = True
        for dctOrderContent in lstContent:
            try:
                docOrder = _upsert_order(dctOrderContent, strCompany)
                _upsert_payload(docOrder, dctOrderContent)
                dFetched += 1
            except Exception:
                frappe.log_error(
                    "Trendyol poll_orders — order upsert failed",
                    frappe.get_traceback(),
                )
                dFailed += 1

            strLastModMs = dctOrderContent.get("lastModifiedDate")
            if strLastModMs:
                try:
                    dtMod = datetime.fromtimestamp(int(strLastModMs) / 1000)
                    if dtLastFetch is None or dtMod > dtLastFetch:
                        blnAllOld = False
                except (ValueError, TypeError, OSError):
                    blnAllOld = False

        if blnAllOld and dtLastFetch is not None:
            blnStopped = True
            break

        dPage += 1

    if not blnStopped and not blnError:
        try:
            docSettings.db_set("last_order_fetch", datetime.now().isoformat(timespec="seconds"))
        except Exception:
            frappe.log_error(
                "Trendyol poll_orders — failed to update last_order_fetch",
                frappe.get_traceback(),
            )

    return dFetched, dFailed


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
            docSettings, "GET", strUrl, dctHeaders, dctParams,
            strApiKey, strApiSecret,
            dctResponse.status_code, dctResponse.text,
        )
    except requests.exceptions.RequestException:
        _log_trendyol_call(
            docSettings, "GET", strUrl, dctHeaders, dctParams,
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
def process_staged_orders(limit=50):
    """Pick up New or Failed staged orders and create Sales Orders from them."""
    lstOrders = frappe.get_all(
        "Trendyol Order",
        filters={
            "status": ["in", ("New", "Failed")],
            "retry_count": ["<", 3],
        },
        order_by="creation asc",
        limit=int(limit),
        pluck="name",
    )

    dProcessed = 0
    dFailed = 0

    blnSettingsAvailable = True
    try:
        docSettings = frappe.get_single("Trendyol Settings")
    except frappe.DoesNotExistError:
        blnSettingsAvailable = False
        docSettings = None

    if blnSettingsAvailable:
        for strOrderName in lstOrders:
            docOrder = frappe.get_doc("Trendyol Order", strOrderName)
            try:
                _process_single_order(docOrder, docSettings)
                dProcessed += 1
            except Exception:
                frappe.log_error(
                    "Trendyol process_staged_orders — single order failed",
                    frappe.get_traceback(),
                )
                dFailed += 1

    return frappe._dict({
        "op_result": blnSettingsAvailable,
        "op_message": "" if blnSettingsAvailable else "Trendyol Settings not configured.",
        "processed": dProcessed,
        "failed": dFailed,
    })


def _process_single_order(docOrder, docSettings):
    """Convert a single staged Trendyol Order into an ERPNext Sales Order."""
    docOrder.status = "Processing"
    docOrder.save(ignore_permissions=True)

    try:
        strCustomer = docSettings.default_customer or "Guest"

        docSO = frappe.new_doc("Sales Order")
        docSO.customer = strCustomer
        docSO.company = docSettings.company
        docSO.transaction_date = docOrder.order_date or datetime.now().date()

        for dctItem in docOrder.items:
            docSO.append("items", {
                "item_name": dctItem.product_name,
                "qty": dctItem.quantity,
                "rate": dctItem.price,
                "amount": dctItem.amount,
                "warehouse": docSettings.default_warehouse,
            })

        if docSettings.tax_mapping:
            for dctTaxMap in docSettings.tax_mapping:
                docSO.append("taxes", {
                    "charge_type": "On Net Total",
                    "rate": dctTaxMap.vat_rate,
                    "account_head": dctTaxMap.account_head,
                })

        docSO.insert(ignore_permissions=True)
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
