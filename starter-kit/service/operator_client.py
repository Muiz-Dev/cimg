import os
import httpx
import logging

logger = logging.getLogger(__name__)

OPERATOR_URL = os.getenv("OPERATOR_URL", "http://operator:9000")
OPERATOR_API_KEY = os.getenv("OPERATOR_API_KEY", "sk_test_operator_7f3a9c1e")


def normalize_msisdn(msisdn: str) -> str:
    cleaned = msisdn.strip()
    if cleaned.startswith("+234"):
        return "0" + cleaned[4:]
    if cleaned.startswith("234"):
        return "0" + cleaned[3:]
    return cleaned


def detect_network(msisdn: str) -> str:
    cleaned = normalize_msisdn(msisdn)
    if len(cleaned) >= 4:
        prefix = cleaned[:4]
        if prefix in {"0803", "0806", "0703", "0706", "0813", "0816", "0810", "0814", "0903", "0906"}:
            return "MTN"
        elif prefix in {"0802", "0808", "0708", "0812", "0701", "0902", "0901", "0904"}:
            return "AIRTEL"
        elif prefix in {"0805", "0807", "0705", "0815", "0811", "0905"}:
            return "GLO"
        elif prefix in {"0809", "0817", "0818", "0909", "0908"}:
            return "9MOBILE"
    return "MTN"


class OperatorClient:
    def __init__(self, base_url: str = OPERATOR_URL, api_key: str = OPERATOR_API_KEY):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    async def vend(self, client_ref: str, msisdn: str, network: str, amount_minor: int) -> dict:
        url = f"{self.base_url}/v1/vend"
        payload = {
            "client_ref": client_ref,
            "msisdn": normalize_msisdn(msisdn),
            "network": network,
            "amount_minor": amount_minor,
        }
        headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

        try:
            # 6 second timeout ensures USSD response returns well within gateway 8 second limit
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.post(url, json=payload, headers=headers)

                if res.status_code == 200:
                    data = res.json()
                    return {
                        "status": data.get("status", "SUCCESSFUL"),
                        "operator_ref": data.get("operator_ref"),
                        "reason_code": data.get("reason_code"),
                    }
                elif res.status_code in (400, 429):
                    try:
                        data = res.json()
                    except Exception:
                        data = {}
                    return {
                        "status": "REJECTED",
                        "operator_ref": None,
                        "reason_code": data.get("reason_code", f"HTTP_{res.status_code}"),
                    }
                elif res.status_code == 504:
                    return {
                        "status": "UNKNOWN",
                        "operator_ref": None,
                        "reason_code": "GATEWAY_TIMEOUT",
                    }
                else:
                    return {
                        "status": "UNKNOWN",
                        "operator_ref": None,
                        "reason_code": f"HTTP_{res.status_code}",
                    }
        except httpx.TimeoutException:
            logger.warning(f"Timeout calling operator for client_ref {client_ref}")
            return {
                "status": "UNKNOWN",
                "operator_ref": None,
                "reason_code": "CLIENT_TIMEOUT",
            }
        except Exception as e:
            logger.error(f"Error calling operator for client_ref {client_ref}: {e}")
            return {
                "status": "UNKNOWN",
                "operator_ref": None,
                "reason_code": str(e),
            }
