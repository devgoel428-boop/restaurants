import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import uuid
import json
import os
import io
import qrcode
from datetime import datetime, timedelta
from typing import Dict, List
from starlette.middleware.sessions import SessionMiddleware
from starlette.config import Config
from authlib.integrations.starlette_client import OAuth
from jose import JWTError, jwt

from sqlalchemy.orm import Session
from models import Base, get_db, Restaurant, MenuItem, Order
from security import generate_qr_signature, verify_qr_signature

class CartItem(BaseModel):
    name: str
    price: float
    quantity: int = 1

class PlaceOrderReq(BaseModel):
    restaurant_id: str
    table_number: str
    items: List[CartItem]
    total_price: float

app = FastAPI(title="QRSnap Multi-tenant Platform")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET", "super-secret-key"))

# --- OAUTH SETUP ---
oauth = OAuth(Config('.env'))
oauth.register(
    name='google',
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# JWT Settings
SECRET_KEY = os.environ.get("JWT_SECRET", "jwt-super-secret-key")
ALGORITHM = "HS256"

def create_access_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Mount Static Files and Templates
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))



# --- OAUTH ROUTES ---

@app.get("/auth/login/google")
async def login_via_google(request: Request):
    # Redirect to Google's OAuth consent screen
    redirect_uri = request.url_for('auth_callback_google')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback/google")
async def auth_callback_google(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token.get('userinfo')
        if not user_info or not user_info.get("email"):
            raise HTTPException(status_code=400, detail="Failed to fetch email from Google")
        
        email = user_info["email"]
        name = user_info.get("name", "Restaurant Owner")
        
        # Check if restaurant exists, otherwise register
        restaurant = db.query(Restaurant).filter(Restaurant.owner_email == email).first()
        if not restaurant:
            restaurant = Restaurant(
                name=f"{name}'s Restaurant",
                owner_email=email,
                hashed_password="oauth_managed",
                secret_salt=uuid.uuid4().hex
            )
            db.add(restaurant)
            db.commit()
            db.refresh(restaurant)

        # Create JWT for owner session
        access_token_expires = timedelta(days=7)
        access_token = create_access_token(
            data={"sub": email, "restaurant_id": str(restaurant.id)}, 
            expires_delta=access_token_expires
        )
        
        # Here we redirect to dashboard and set the JWT cookie
        response = RedirectResponse(url="/dashboard")
        response.set_cookie(key="qrsnap_token", value=access_token, httponly=True)
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- HTTP ROUTES ---

@app.post("/admin/seed")
async def seed_database(db: Session = Depends(get_db)):
    # Check if a test restaurant already exists
    existing = db.query(Restaurant).filter(Restaurant.owner_email == "test@owner.com").first()
    if existing:
        return {"message": "Database already seeded!", "restaurant_id": str(existing.id)}
        
    # Create test restaurant
    res = Restaurant(
        name="The Great Indian Kitchen",
        owner_email="test@owner.com",
        hashed_password="hashed_pass_placeholder",
        secret_salt=uuid.uuid4().hex
    )
    db.add(res)
    db.commit()
    db.refresh(res)
    
    # Add menu items
    items = [
        MenuItem(restaurant_id=res.id, name="Paneer Butter Masala", price=250.0, category="Main Course", is_veg=True),
        MenuItem(restaurant_id=res.id, name="Chicken Tikka", price=300.0, category="Starters", is_veg=False),
        MenuItem(restaurant_id=res.id, name="Garlic Naan", price=50.0, category="Breads", is_veg=True),
        MenuItem(restaurant_id=res.id, name="Gulab Jamun", price=90.0, category="Desserts", is_veg=True)
    ]
    db.add_all(items)
    db.commit()
    
    return {"message": "Success! Database seeded.", "restaurant_id": str(res.id)}

@app.get("/admin/qr")
async def generate_qr(request: Request, restaurant_id: str, table: str = "1", db: Session = Depends(get_db)):
    try:
        r_uuid = uuid.UUID(restaurant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")
        
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")
        
    sig = generate_qr_signature(restaurant_id, table, restaurant.secret_salt)
    url = f"{request.base_url}order/{restaurant_id}?table={table}&sig={sig}"
    
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    
    return StreamingResponse(buf, media_type="image/png")

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/order/{restaurant_id}", response_class=HTMLResponse)
async def customer_menu(request: Request, restaurant_id: str, table: str = "1", sig: str = "", db: Session = Depends(get_db)):
    if restaurant_id == "demo":
        # Mock payload for the demonstration front-end link
        items = [{"name": "Paneer Butter Masala", "price": 250, "is_veg": True}, {"name": "Chicken Tikka", "price": 300, "is_veg": False}]
        return templates.TemplateResponse("menu_v2.html", {"request": request, "restaurant_id": "demo", "table": "1", "items": items})
    
    # 1. Fetch Restaurant & Secret Salt
    try:
        r_uuid = uuid.UUID(restaurant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Restaurant ID format")
        
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    # 2. Verify HMAC-SHA256 Signature
    if not verify_qr_signature(restaurant_id, table, sig, restaurant.secret_salt):
        raise HTTPException(status_code=403, detail="Invalid QR Signature. Please scan the official QR code at your table.")

    # 3. Fetch active menu items
    menu_items = db.query(MenuItem).filter(MenuItem.restaurant_id == r_uuid, MenuItem.is_available == True).all()

    return templates.TemplateResponse("menu_v2.html", {
        "request": request, 
        "restaurant_id": restaurant_id, 
        "table": table,
        "items": menu_items
    })

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/kds/{restaurant_id}", response_class=HTMLResponse)
async def kds_display(request: Request, restaurant_id: str, db: Session = Depends(get_db)):
    active_orders = []
    if restaurant_id != "demo":
        try:
            r_uuid = uuid.UUID(restaurant_id)
            active_orders = db.query(Order).filter(Order.restaurant_id == r_uuid, Order.status == "pending").all()
        except Exception:
            pass
    return templates.TemplateResponse("kds_mobile.html", {"request": request, "restaurant_id": restaurant_id, "orders": active_orders})

# --- SERVERLESS KDS POLLING ROUTES ---
@app.get("/api/kds/orders/{restaurant_id}")
async def get_kds_orders(restaurant_id: str, db: Session = Depends(get_db)):
    if restaurant_id == "demo":
        return {"orders": []}
    try:
        r_uuid = uuid.UUID(restaurant_id)
        orders = db.query(Order).filter(Order.restaurant_id == r_uuid, Order.status == "pending").all()
        return {"orders": [
            {
                "id": str(o.id),
                "table_number": o.table_number,
                "items": o.items
            } for o in orders
        ]}
    except Exception:
        return {"orders": []}

@app.post("/api/kds/bump/{ticket_id}")
async def bump_order(ticket_id: str, db: Session = Depends(get_db)):
    if ticket_id == "demo1" or ticket_id == "demo":
        return {"status": "success"}
    try:
        order = db.query(Order).filter(Order.id == uuid.UUID(ticket_id)).first()
        if order:
            order.status = "done"
            db.commit()
    except Exception:
        raise HTTPException(status_code=400, detail="Error bumping ticket")
    return {"status": "success"}

@app.post("/api/order/place")
async def place_order(req: PlaceOrderReq, db: Session = Depends(get_db)):
    try:
        r_uuid = uuid.UUID(req.restaurant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid restaurant_id")
    
    order = Order(
        restaurant_id=r_uuid,
        table_number=req.table_number,
        items=[item.dict() for item in req.items],
        total_price=req.total_price,
        status="pending"
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    

    
    return {"status": "success", "ticket_id": str(order.id)}

# --- AI COMMAND CENTER ---
class VoiceCommandReq(BaseModel):
    restaurant_id: str
    transcript: str

@app.post("/admin/voice-command")
async def handle_voice_command(req: VoiceCommandReq):
    # Uses OpenAI Function Calling in production to parse transcript and output DB updates
    # Mocking successful DB update for UI demonstration
    return {
        "status": "success", 
        "parsed_action": "update_price", 
        "item": "Pizza", 
        "new_price": 500,
        "message": "Updated 'Pizza' price to ₹500 successfully."
    }