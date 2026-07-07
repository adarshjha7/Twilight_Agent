import httpx
from loguru import logger
from config import config


async def submit_bill(bill: dict) -> dict:
    # Strip internal meta before sending
    payload = {k: v for k, v in bill.items() if k != "_meta"}
    url = f"{config.website_api_base_url}{config.website_bill_endpoint}"

    logger.info(f"Submitting bill → {url}  vendor={bill.get('vendor_name')}  total={bill.get('total_amount')}")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {config.website_api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()

    result = response.json()
    logger.info(f"Bill submitted — status {response.status_code}")
    return result
