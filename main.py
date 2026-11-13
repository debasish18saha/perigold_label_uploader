import os
import io
import sys
import json
import logging
import fnmatch
from datetime import datetime, timezone
import requests
import paramiko

# ==================== CONFIGURATION ====================
# SFTP Configuration
SFTP_HOST = "192.168.52.26"
SFTP_PORT = 22
SFTP_USER = "ukftp"
SFTP_PASS = "ukftp"
SFTP_DIRECTORY = "/sapmnt/WPR/wuk_data/download/Perigold/perisoapbox"

# Wayfair (Perigold) API Configuration
WAYFAIR_SSO_URL = "https://sso.auth.wayfair.com/oauth/token"
WAYFAIR_GRAPHQL_URL = "https://api.wayfair.com/v1/graphql"
WAYFAIR_LABEL_URL = "https://api.wayfair.com/v1/shipping_label"
WAYFAIR_CLIENT_ID = "OqgDMetyZVTrRqykSTglyw"
WAYFAIR_CLIENT_SECRET = "0Eg2YqpAlQLTVp1jksfVKG0RYGX62BA4P9dsfkVkIl5bZTw-iXjQzzaPnUXOquXsxKZUJjsGpfcu8ChGOUK-lg"
TOKEN_CACHE_FILE = "wayfair_token_cache.json"

# SoapBox API Configuration
SOAPBOX_UPLOAD_URL = "https://api.soap-bx.com/api/v2/attachments/order-documents/"
SOAPBOX_API_KEY = "8]WTnjCwD%10)=p4tF6H"
SOAPBOX_ORG_ID = "26932"
SOAPBOX_CATEGORY = "shipment_pallet_pack_list" #"commercial_invoice"

LOG_FILE = "perigold_label_uploader.log"
# =======================================================


def setup_logger():
    """Configure dual logging to file and standard output."""
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)


# =======================================================
# WAYFAIR TOKEN MANAGEMENT
# =======================================================

def get_valid_wayfair_token():
    """Retrieve Wayfair Bearer token. Checks cache first; generates new if expired."""
    if os.path.exists(TOKEN_CACHE_FILE):
        try:
            with open(TOKEN_CACHE_FILE, "r") as f:
                cache = json.load(f)
                cached_time = datetime.fromisoformat(cache.get("timestamp"))
                now = datetime.now(timezone.utc)
                age_in_hours = (now - cached_time).total_seconds() / 3600.0

                if age_in_hours < 23.5:
                    logging.info("Using active cached Wayfair Bearer token.")
                    return cache.get("access_token")
        except Exception as e:
            logging.warning(f"Could not read token cache: {str(e)}")

    return generate_new_wayfair_token()


def generate_new_wayfair_token():
    """Request a fresh Bearer token from Wayfair SSO API."""
    logging.info("Generating a NEW Wayfair Bearer Token...")
    payload = {
        "grant_type": "client_credentials",
        "client_id": WAYFAIR_CLIENT_ID,
        "client_secret": WAYFAIR_CLIENT_SECRET
    }

    try:
        response = requests.post(WAYFAIR_SSO_URL, json=payload)
        if response.status_code == 200:
            token_data = response.json()
            access_token = token_data.get("access_token")

            # Cache the new token
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({
                    "access_token": access_token,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }, f)

            logging.info("--> Successfully generated and cached new Wayfair token.")
            return access_token
        else:
            logging.error(f"--> Failed to generate Wayfair token. HTTP {response.status_code}: {response.text}")
            return None
    except Exception as e:
        logging.error(f"--> Exception generating Wayfair token: {str(e)}")
        return None


# =======================================================
# WAYFAIR API (REGISTRATION & LABEL DOWNLOAD)
# =======================================================

def register_perigold_order(po_number, access_token):
    """Register the order using Wayfair GraphQL API."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Format Date: current date in expected EST payload string format (YYYY-MM-DD 00:00:00.000000 -05:00)
    current_date_est = datetime.now().strftime("%Y-%m-%d 00:00:00.000000 -05:00")

    graphql_query = """
    mutation register($registrationInput: RegistrationInput!) {
        purchaseOrders {
            register(registrationInput: $registrationInput) {
                id
                poNumber
                generatedShippingLabels {
                    poNumber
                    trackingNumber
                }
            }
        }
    }
    """

    variables = {
        "registrationInput": {
            "poNumber": po_number,
            "warehouseId": 20738,
            "requestForPickupDate": current_date_est
        }
    }

    payload = {
        "query": graphql_query,
        "variables": variables
    }

    try:
        logging.info(f"Registering Perigold Order: {po_number}...")
        response = requests.post(WAYFAIR_GRAPHQL_URL, headers=headers, json=payload)

        if response.status_code == 200:
            json_response = response.json()
            if "errors" in json_response:
                logging.error(f"--> GraphQL Error for Order {po_number}: {json_response['errors']}")
                return False

            logging.info(f"--> SUCCESS: Order {po_number} successfully registered with Wayfair.")
            return True
        else:
            logging.error(f"--> FAILED to register Order {po_number}. HTTP {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logging.error(f"--> Exception registering Order {po_number}: {str(e)}")
        return False


def download_perigold_label(po_number, access_token):
    """Download the generated shipping label PDF from Wayfair REST API."""
    url = f"{WAYFAIR_LABEL_URL}/{po_number}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/pdf"
    }

    try:
        logging.info(f"Downloading Shipping Label for PO: {po_number}...")
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            pdf_bytes = response.content
            logging.info(f"--> SUCCESS: Shipping Label downloaded ({len(pdf_bytes)} bytes).")
            return pdf_bytes
        else:
            logging.error(f"--> FAILED to download label for {po_number}. HTTP {response.status_code}: {response.text}")
            return None
    except Exception as e:
        logging.error(f"--> Exception downloading label for {po_number}: {str(e)}")
        return None


# =======================================================
# SOAPBOX API (DOCUMENT UPLOAD)
# =======================================================

def upload_to_soapbox(soapbox_id, pdf_bytes, filename):
    """Upload downloaded PDF label to SoapBox."""
    headers = {
        "api-key": SOAPBOX_API_KEY,
        "org-id": SOAPBOX_ORG_ID
    }

    form_data = {
        "order": str(soapbox_id),
        "category": SOAPBOX_CATEGORY
    }

    files = {
        "file": (filename, io.BytesIO(pdf_bytes), "application/pdf")
    }

    try:
        logging.info(f"Uploading Label to SoapBox (SoapBox ID: {soapbox_id}, File: {filename})...")
        response = requests.post(SOAPBOX_UPLOAD_URL, headers=headers, data=form_data, files=files)

        if response.status_code in [200, 201]:
            logging.info(f"--> SUCCESS (HTTP {response.status_code}): Label attached to SoapBox ID {soapbox_id}.")
            return True
        else:
            logging.error(f"--> FAILED to upload to SoapBox. HTTP {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logging.error(f"--> Exception uploading to SoapBox: {str(e)}")
        return False


# =======================================================
# SFTP FILE PROCESSING & WORKFLOW ORCHESTRATION
# =======================================================

def parse_csv_mapping(csv_text):
    """Parse semicolon-separated SFTP input file."""
    lines = [line.strip() for line in csv_text.splitlines() if line.strip()]
    if len(lines) <= 1:
        logging.warning("CSV file contains only a header or is empty.")
        return []

    records = []
    # Skip header row
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(";") if p.strip()]
        if len(parts) >= 3:
            soapbox_id = parts[0]
            sap_delivery = parts[1]
            peri_order_id = parts[2]
            records.append((soapbox_id, sap_delivery, peri_order_id))

    return records


def process_sftp_files():
    """Connect to SFTP via Paramiko, orchestrate the API pipeline, and rename finished files."""

    # Pre-fetch a valid API token before orchestrating the queue
    wayfair_access_token = get_valid_wayfair_token()
    if not wayfair_access_token:
        logging.error("CRITICAL: Failed to acquire valid Wayfair Bearer token. Aborting run.")
        return

    logging.info(f"Connecting to SFTP server '{SFTP_HOST}'...")
    try:
        # Establish SFTP Connection using Paramiko
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASS)
        sftp = paramiko.SFTPClient.from_transport(transport)

        sftp.chdir(SFTP_DIRECTORY)
        file_list = sftp.listdir()

        # Locate files matching 'periordersmap*.csv' (or 'Perigold*') and skip processed
        unprocessed_files = [
            f for f in file_list
            if not f.startswith("processed_") and (
                    fnmatch.fnmatch(f, "periordersmap*.csv") or fnmatch.fnmatch(f, "Perigold*.csv")
            )
        ]

        if not unprocessed_files:
            logging.info("No matching unprocessed mapping files found on SFTP server.")
            sftp.close()
            transport.close()
            return

        logging.info(f"Found {len(unprocessed_files)} unprocessed file(s).")

        for filename in unprocessed_files:
            logging.info(f"\n--- Processing File: {filename} ---")

            # Stream SFTP file into memory
            file_obj = io.BytesIO()
            sftp.getfo(filename, file_obj)
            csv_text = file_obj.getvalue().decode("utf-8-sig")

            records = parse_csv_mapping(csv_text)
            if not records:
                logging.error(f"No valid mapping records found in '{filename}'. Skipping file.")
                continue

            file_overall_success = True

            for soapbox_id, sap_delivery_no, po_number in records:
                logging.info(f"Record -> SoapBox ID: {soapbox_id} | PO Number: {po_number}")

                # Step 1: Register Order
                if not register_perigold_order(po_number, wayfair_access_token):
                    file_overall_success = False
                    continue

                # Step 2: Download Label PDF
                pdf_bytes = download_perigold_label(po_number, wayfair_access_token)
                if not pdf_bytes:
                    file_overall_success = False
                    continue

                # Step 3: Upload to SoapBox
                soapbox_filename = f"Perigold_Label_{po_number}.pdf"
                if not upload_to_soapbox(soapbox_id, pdf_bytes, soapbox_filename):
                    file_overall_success = False

            # Rename file to processed_... if all records succeeded
            if file_overall_success:
                processed_filename = f"processed_{filename}"
                sftp.rename(filename, processed_filename)
                logging.info(f"Successfully processed and renamed SFTP file to: '{processed_filename}'")
            else:
                logging.warning(f"File '{filename}' encountered errors during processing and was NOT renamed.")

        sftp.close()
        transport.close()

    except Exception as e:
        logging.error(f"SFTP Processing Error: {str(e)}")


def main():
    """Execution entry point."""
    setup_logger()
    logging.info("=== Perigold Label Uploader Run Started ===")
    process_sftp_files()
    logging.info("=== Perigold Label Uploader Run Ended ===\n")


if __name__ == "__main__":
    main()