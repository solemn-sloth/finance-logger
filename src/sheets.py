"""
Google Sheets — read and write via service account auth.

GOOGLE_SA_KEY_PATH in config/.env points to the service account JSON file.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def get_sheet_service():
    """Build and return an authenticated Google Sheets API service."""
    key_path = os.environ["GOOGLE_SA_KEY_PATH"]
    credentials = service_account.Credentials.from_service_account_file(
        key_path, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def read_cell(sheet_id: str, tab: str, cell: str) -> str:
    """
    Read a single cell value.

    Args:
        sheet_id: The Google Sheet ID (from the URL).
        tab: Sheet tab name (e.g. "Finance").
        cell: A1 notation (e.g. "B2").

    Returns:
        Cell value as a string, or "" if empty.
    """
    service = get_sheet_service()
    range_ = f"'{tab}'!{cell}"
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=range_)
        .execute()
    )
    values = result.get("values", [])
    if not values or not values[0]:
        return ""
    return values[0][0]


def append_row(sheet_id: str, tab: str, values: list) -> None:
    """
    Append a row to the next empty line in the sheet.

    Args:
        sheet_id: The Google Sheet ID.
        tab: Sheet tab name.
        values: List of values for the row (strings, ints, or floats).
    """
    service = get_sheet_service()
    range_ = f"'{tab}'!A1"
    body = {"values": [values]}
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=range_,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()


def update_cell(sheet_id: str, tab: str, cell: str, value) -> None:
    """
    Write a value to a specific cell.

    Args:
        sheet_id: The Google Sheet ID.
        tab: Sheet tab name.
        cell: A1 notation (e.g. "B2").
        value: Value to write.
    """
    service = get_sheet_service()
    range_ = f"'{tab}'!{cell}"
    body = {"values": [[value]]}
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_,
        valueInputOption="USER_ENTERED",
        body=body,
    ).execute()
