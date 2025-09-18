# lambdas/roombooking/handler.py
import json
import os
import uuid
from datetime import datetime, timedelta

import boto3
from botocore.exceptions import ClientError

AVAIL_TABLE = os.getenv("AVAIL_TABLE", "hotelRoomAvailabilityTable")
BOOKINGS_TABLE = os.getenv("BOOKINGS_TABLE", "hotelRoomBookingTable")

ddb = boto3.client("dynamodb")


def _body_from_event(event: dict) -> dict:
    """
    Bedrock Agents usually send:
      event["requestBody"]["content"]["application/json"]  -> dict
    …but some UIs send:
      …["application/json"]["properties"] -> [{name,value}, …]
    Handle both.
    """
    rb = (event.get("requestBody") or {}).get("content", {}).get("application/json")
    if isinstance(rb, dict) and "properties" in rb and isinstance(rb["properties"], list):
        return {p.get("name"): p.get("value") for p in rb["properties"]}
    return rb or {}


def _normalize_room_type(rt: str | None) -> str | None:
    if not rt:
        return None
    s = rt.strip().lower().replace("_", "").replace(" ", "")
    if s in {"seaview", "sea", "seaviewroom"}:
        return "seaView"
    if s in {"gardenview", "garden", "gardenviewroom"}:
        return "gardenView"
    return None


def _date_seq(start_yyyy_mm_dd: str, nights: int) -> list[str]:
    d0 = datetime.strptime(start_yyyy_mm_dd, "%Y-%m-%d").date()
    return [(d0 + timedelta(days=i)).isoformat() for i in range(nights)]


def _resp(event: dict, status: int, body: dict):
    # Bedrock expects the Lambda integration envelope.
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", "Hotel Booking API"),
            "apiPath": event.get("apiPath", "/bookHotelRoom"),
            "httpMethod": event.get("httpMethod", "POST"),
            "httpStatusCode": status,
            "responseBody": {"application/json": {"body": json.dumps(body)}},
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


def lambda_handler(event, _context):
    print(f"Incoming event: {json.dumps(event)}")

    body = _body_from_event(event)
    guestName = body.get("guestName")
    checkInDate = body.get("checkInDate")
    numberofNights_raw = body.get("numberofNights")
    roomType_in = body.get("roomType")

    if not all([guestName, checkInDate, numberofNights_raw, roomType_in]):
        return _resp(event, 400, {"error": "Missing guestName, checkInDate, numberofNights or roomType"})

    try:
        nights = int(str(numberofNights_raw))
        if nights <= 0:
            raise ValueError
    except ValueError:
        return _resp(event, 400, {"error": "numberofNights must be a positive integer"})

    if nights > 24:
        return _resp(event, 400, {"error": "numberofNights too large for a single transaction (max 24)"})

    try:
        datetime.strptime(checkInDate, "%Y-%m-%d")
    except Exception:
        return _resp(event, 400, {"error": "checkInDate must be YYYY-MM-DD"})

    roomType = _normalize_room_type(roomType_in)
    if roomType not in {"seaView", "gardenView"}:
        return _resp(event, 400, {"error": "roomType must be 'seaView' or 'gardenView'"})

    stay_dates = _date_seq(checkInDate, nights)

    # Pre-read & validate availability
    snapshots, missing, insufficient = {}, [], []
    for d in stay_dates:
        r = ddb.get_item(TableName=AVAIL_TABLE, Key={"date": {"S": d}})
        if "Item" not in r or roomType not in r["Item"]:
            missing.append(d)
            continue
        try:
            curr = int(r["Item"][roomType]["S"])
        except Exception:
            return _resp(event, 500, {"error": f"Invalid {roomType} value on {d}. Expected stringified int"})
        if curr < 1:
            insufficient.append({"date": d, "available": curr})
        else:
            snapshots[d] = {"old_str": r["Item"][roomType]["S"], "old_int": curr}

    if missing:
        return _resp(event, 404, {"error": "No availability record for some dates", "missingDates": missing})
    if insufficient:
        return _resp(event, 404, {"error": "Insufficient availability", "details": insufficient})

    # Build transaction: nightly decrements + booking put
    bookingID = str(uuid.uuid4())
    tx = []

    for d in stay_dates:
        snap = snapshots[d]
        new_int = snap["old_int"] - 1
        if new_int < 0:
            return _resp(event, 409, {"error": "Race detected: availability changed, please retry"})

        tx.append({
            "Update": {
                "TableName": AVAIL_TABLE,
                "Key": {"date": {"S": d}},
                "ConditionExpression": "#rt = :old",
                "UpdateExpression": "SET #rt = :new",
                "ExpressionAttributeNames": {"#rt": roomType},
                "ExpressionAttributeValues": {":old": {"S": snap["old_str"]}, ":new": {"S": str(new_int)}},
            }
        })

    booking_item = {
        "bookingID": {"S": bookingID},
        "guestName": {"S": guestName},
        "checkInDate": {"S": checkInDate},
        "numberofNights": {"S": str(nights)},
        "roomType": {"S": roomType},
        "stayDates": {"L": [{"S": d} for d in stay_dates]},
    }
    tx.append({"Put": {"TableName": BOOKINGS_TABLE, "Item": booking_item, "ConditionExpression": "attribute_not_exists(bookingID)"}})

    try:
        ddb.transact_write_items(TransactItems=tx)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            return _resp(event, 409, {"error": "Availability changed during booking. Please retry."})
        return _resp(event, 500, {"error": "Transaction failed", "details": str(e)})

    return _resp(event, 200, {
        "returnBookingID": bookingID,
        "guestName": guestName,
        "checkInDate": checkInDate,
        "numberofNights": nights,
        "roomType": roomType,
        "reservedDates": stay_dates,
    })
