import frappe
import requests
from requests.auth import HTTPBasicAuth


def _log_trendyol_call(docSettings, strMethod, strUrl, dctHeaders, dctParams, strApiKey, strApiSecret, dStatusCode=None, strResponseBody=None):
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


@frappe.whitelist()
def check_connection(docname):
    docSettings = frappe.get_doc("Trendyol Settings", docname)
    strSupplierId = docSettings.supplier_id
    strApiKey = docSettings.get_password("api_key")
    strApiSecret = docSettings.get_password("api_secret")

    strBaseUrl = docSettings.service_url.rstrip("/")
    strUrl = f"{strBaseUrl}/suppliers/{strSupplierId}/orders"
    dctParams = {
        "orderByField": "PackageLastModifiedDate",
        "orderByDirection": "DESC",
        "size": 1,
    }
    dctHeaders = {
        "User-Agent": f"{strSupplierId} - SelfIntegration",
        "Accept": "*/*",
    }

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
