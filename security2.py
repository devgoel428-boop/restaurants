import hmac
import hashlib
from urllib.parse import urlencode

def get_secure_qr_link(restaurant_id, table_number, secret_salt):
    base_url = f"https://qrsnap.com/order/{restaurant_id}"
    
    # Create the security signature
    payload = f"{restaurant_id}-{table_number}"
    signature = hmac.new(
        secret_salt.encode(), 
        payload.encode(), 
        hashlib.sha256
    ).hexdigest()
    
    # Generate the full link
    params = {'table': table_number, 'sig': signature}
    return f"{base_url}?{urlencode(params)}"

# Example output for an owner:
# https://qrsnap.com/order/rest_123?table=5&sig=a7b2c9...