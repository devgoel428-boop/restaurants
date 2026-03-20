import hashlib
import hmac
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def generate_qr_signature(restaurant_id: str, table_number: str, secret_salt: str) -> str:
    """Generate HMAC-SHA256 hash for secure QR"""
    message = f"{restaurant_id}-{table_number}".encode('utf-8')
    key = secret_salt.encode('utf-8')
    signature = hmac.new(key, message, hashlib.sha256).hexdigest()
    return signature

def verify_qr_signature(restaurant_id: str, table_number: str, signature: str, secret_salt: str) -> bool:
    """Verify HMAC-SHA256 signature"""
    expected_sig = generate_qr_signature(restaurant_id, table_number, secret_salt)
    return hmac.compare_digest(expected_sig, signature)