"""
Standard HTTP response helpers for Lambda + API Gateway.
"""
import json
import os


def _cors_headers() -> dict:
    frontend = os.environ.get("FRONTEND_URL", "*")
    return {
        "Access-Control-Allow-Origin": frontend,
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Content-Type": "application/json",
    }


def ok(body: dict, status: int = 200) -> dict:
    return {
        "statusCode": status,
        "headers": _cors_headers(),
        "body": json.dumps(body),
    }


def created(body: dict) -> dict:
    return ok(body, 201)


def error(message: str, status: int = 400, details: dict = None) -> dict:
    body = {"error": message}
    if details:
        body["details"] = details
    return {
        "statusCode": status,
        "headers": _cors_headers(),
        "body": json.dumps(body),
    }


def not_found(resource: str = "Resource") -> dict:
    return error(f"{resource} not found", 404)


def server_error(message: str = "Internal server error") -> dict:
    return error(message, 500)
