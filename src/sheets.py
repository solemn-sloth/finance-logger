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


def _get_sheet_gid(sheet_id: str, tab_name: str) -> int:
    """Return the numeric sheet GID for a tab by name."""
    service = get_sheet_service()
    meta = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == tab_name:
            return s["properties"]["sheetId"]
    raise RuntimeError(f"Tab '{tab_name}' not found in sheet {sheet_id}")


def format_column_text_color(
    sheet_id: str, tab: str, col: int, row_start: int, rgb: tuple[float, float, float]
) -> None:
    """Apply static text color to an entire column from row_start (0-indexed)."""
    service = get_sheet_service()
    gid = _get_sheet_gid(sheet_id, tab)
    r, g, b = rgb
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"repeatCell": {
            "range": {
                "sheetId": gid,
                "startRowIndex": row_start,
                "startColumnIndex": col,
                "endColumnIndex": col + 1,
            },
            "cell": {"userEnteredFormat": {"textFormat": {
                "foregroundColor": {"red": r, "green": g, "blue": b}
            }}},
            "fields": "userEnteredFormat.textFormat.foregroundColor",
        }}]},
    ).execute()


def add_conditional_format_positive_negative(
    sheet_id: str, tab: str, row_start: int, col: int
) -> None:
    """Add conditional formatting: green text if >0, red text if <0, from row_start (0-indexed).

    Call once only — each call adds new rules; duplicates accumulate in Sheets.
    """
    service = get_sheet_service()
    gid = _get_sheet_gid(sheet_id, tab)
    cell_range = {
        "sheetId": gid,
        "startRowIndex": row_start,
        "startColumnIndex": col,
        "endColumnIndex": col + 1,
    }
    green = {"red": 0.133, "green": 0.545, "blue": 0.133}
    red = {"red": 0.8, "green": 0.0, "blue": 0.0}
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [
            {"addConditionalFormatRule": {"rule": {
                "ranges": [cell_range],
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": green}},
                },
            }, "index": 0}},
            {"addConditionalFormatRule": {"rule": {
                "ranges": [cell_range],
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                    "format": {"textFormat": {"foregroundColor": red}},
                },
            }, "index": 1}},
        ]},
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
