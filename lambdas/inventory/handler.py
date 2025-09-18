# lambdas/inventory/handler.py
import json
import os
import boto3
from boto3.dynamodb.types import TypeDeserializer
from datetime import datetime

TABLE_NAME = os.environ.get("AVAIL_TABLE", "hotelRoomAvailabilityTable")

ddb = boto3.client("dynamodb")
DESER = TypeDeserializer()


def _ddb_to_plain(item: dict) -> dict:
    return {k: DESER.deserialize(v) for k, v in (item or {}).items()}


def _agent_resp(event: dict, status: int, body: dict):
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", "Hotel Room Inventory API"),
            "apiPath": event.get("apiPath", "/getRoomInventory/{date}"),
            "httpMethod": event.get("httpMethod", "GET"),
            "httpStatusCode": status,
            "responseBody": {"application/json": {"body": json.dumps(body)}},
        },
        "sessionAttributes": event.get("sessionAttributes", {}),
        "promptSessionAttributes": event.get("promptSessionAttributes", {}),
    }


def _get_date(event: dict) -> str | None:
    """
    Bedrock commonly sends:
      event["parameters"]["path"]["date"]  (map form)
    Some tools send:
      event["parameters"][0]["value"]      (array form)
    Handle both.
    """
    params = event.get("parameters") or {}
    if isinstance(params, dict):
        return ((params.get("path") or {}).get("date")) or ((params.get("query") or {}).get("date"))
    if isinstance(params, list) and params and isinstance(params[0], dict):
        return params[0].get("value")
    return None


def lambda_handler(event, _context):
    print(f"Incoming event: {json.dumps(event)}")

    date_str = _get_date(event)
    if not date_str:
        return _agent_resp(event, 400, {"error": "date path parameter is required"})
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return _agent_resp(event, 400, {"error": "invalid date format, expected YYYY-MM-DD"})

    try:
        r = ddb.get_item(TableName=TABLE_NAME, Key={"date": {"S": date_str}})
    except Exception as e:
        return _agent_resp(event, 500, {"error": "DynamoDB get_item failed", "details": str(e)})

    if "Item" not in r:
        return _agent_resp(event, 404, {"message": "No availability found for the requested date", "date": date_str})

    item = _ddb_to_plain(r["Item"])
    # coerce counts to ints if possible
    gv = item.get("gardenView", 0)
    sv = item.get("seaView", 0)
    try: gv = int(gv)
    except Exception: pass
    try: sv = int(sv)
    except Exception: pass

    body = {
        "date": date_str,
        "gardenViewInventory": gv,
        "seaViewInventory": sv,
        "summary": {"totalAvailable": gv + sv if isinstance(gv, int) and isinstance(sv, int) else None},
    }
    return _agent_resp(event, 200, body) 
