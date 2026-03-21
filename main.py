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
async def dashboard(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    restaurant_name = "Demo Restaurant"
    r_id = "demo"
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            r_id = payload.get("restaurant_id")
            r_uuid = uuid.UUID(r_id)
            res = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
            if res: restaurant_name = res.name
        except:
            pass
    return templates.TemplateResponse("dashboard.html", {"request": request, "restaurant_name": restaurant_name, "restaurant_id": r_id})

@app.get("/api/dashboard/stats")
async def get_dashboard_stats(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token:
        return {"revenue": 14500, "active_orders": 12, "qr_scans": 84, "peak_hours": [10, 25, 40, 50, 90, 40, 20]}
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except:
        return {"revenue": 0, "active_orders": 0, "qr_scans": 0, "peak_hours": [0]*7}
        
    orders = db.query(Order).filter(Order.restaurant_id == r_uuid).all()
    revenue = sum(o.total_price for o in orders if o.status == "done")
    active = sum(1 for o in orders if o.status == "pending")
    # Mocking peak hours and scans for now based on total orders
    base_scans = len(orders) * 3 if orders else 15
    return {
        "revenue": revenue,
        "active_orders": active,
        "qr_scans": base_scans,
        "peak_hours": [5, 12, 18, 45, 80, 55, 20] # Beautiful bell curve for ChartJS
    }

@app.get("/auth/mock-login")
async def mock_login(request: Request, db: Session = Depends(get_db)):
    email = "test@owner.com"
    restaurant = db.query(Restaurant).filter(Restaurant.owner_email == email).first()
    if not restaurant:
        restaurant = Restaurant(name="Mock Restaurant", owner_email=email, hashed_password="mock", secret_salt=uuid.uuid4().hex)
        db.add(restaurant)
        db.commit()
        db.refresh(restaurant)
    
    access_token = create_access_token(data={"sub": email, "restaurant_id": str(restaurant.id)}, expires_delta=timedelta(days=7))
    response = RedirectResponse(url="/dashboard")
    response.set_cookie(key="qrsnap_token", value=access_token, httponly=True)
    return response

# --- MENU MANAGER ROUTES ---
class MenuItemReq(BaseModel):
    name: str
    price: float
    category: str
    is_veg: bool

@app.get("/menu-manager", response_class=HTMLResponse)
async def menu_manager(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token:
        return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except:
        return RedirectResponse(url="/")
    
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    items = db.query(MenuItem).filter(MenuItem.restaurant_id == r_uuid).all()
    # Sort by category for UI
    from collections import defaultdict
    categorized_items = defaultdict(list)
    for i in items:
        categorized_items[i.category].append(i)
        
    return templates.TemplateResponse("menu_manager.html", {
        "request": request, 
        "restaurant_id": str(r_uuid), 
        "restaurant_name": restaurant.name, 
        "categories": dict(categorized_items)
    })

@app.post("/api/menu")
async def add_menu_item(request: Request, item: MenuItemReq, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: raise HTTPException(status_code=401)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except: raise HTTPException(status_code=401)
    
    new_item = MenuItem(restaurant_id=r_uuid, name=item.name, price=item.price, category=item.category, is_veg=item.is_veg, is_available=True)
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    return {"status": "success", "item": {"id": str(new_item.id), "name": new_item.name, "price": new_item.price}}

@app.put("/api/menu/{item_id}/toggle-availability")
async def toggle_availability(item_id: str, request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: raise HTTPException(status_code=401)
    try: i_uuid = uuid.UUID(item_id)
    except: raise HTTPException(status_code=400)
    
    menu_item = db.query(MenuItem).filter(MenuItem.id == i_uuid).first()
    if not menu_item: raise HTTPException(status_code=404)
    
    menu_item.is_available = not menu_item.is_available
    db.commit()
    return {"status": "success", "is_available": menu_item.is_available}

@app.delete("/api/menu/{item_id}")
async def delete_menu_item(item_id: str, request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: raise HTTPException(status_code=401)
    try: i_uuid = uuid.UUID(item_id)
    except: raise HTTPException(status_code=400)
    
    menu_item = db.query(MenuItem).filter(MenuItem.id == i_uuid).first()
    if menu_item:
        db.delete(menu_item)
        db.commit()
    return {"status": "success"}

# --- BULK QR GENERATOR ROUTES ---
@app.get("/qr-generator", response_class=HTMLResponse)
async def qr_generator_page(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except: return RedirectResponse(url="/")
    
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    return templates.TemplateResponse("qr_generator.html", {"request": request, "restaurant_id": str(r_uuid), "restaurant_name": restaurant.name})

@app.get("/print-qr", response_class=HTMLResponse)
async def print_qr(request: Request, count: int = 10, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except: return RedirectResponse(url="/")
    
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    if not restaurant: return RedirectResponse(url="/")
    
    import base64
    tables = []
    
    # We will use the base_url for the generated QR links
    base_url = str(request.base_url).rstrip('/')
    
    for i in range(1, count + 1):
        table_no = str(i)
        sig = generate_qr_signature(str(r_uuid), table_no, restaurant.secret_salt)
        url = f"{base_url}/order/{str(r_uuid)}?table={table_no}&sig={sig}"
        
        qr = qrcode.QRCode(version=1, box_size=10, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#BC2F32", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
        tables.append({"number": table_no, "qr_b64": b64})
        
    return templates.TemplateResponse("qr_print.html", {"request": request, "restaurant_name": restaurant.name, "tables": tables})

# --- SETTINGS ROUTES ---
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
        email = payload.get("sub")
    except: return RedirectResponse(url="/")
    
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    return templates.TemplateResponse("settings.html", {
        "request": request, 
        "restaurant_name": restaurant.name, 
        "email": email
    })

@app.post("/api/settings/update")
async def update_settings(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("qrsnap_token")
    if not token: return RedirectResponse(url="/")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        r_uuid = uuid.UUID(payload.get("restaurant_id"))
    except: return RedirectResponse(url="/")
    
    restaurant = db.query(Restaurant).filter(Restaurant.id == r_uuid).first()
    if not restaurant: return RedirectResponse(url="/")
    
    form = await request.form()
    new_name = form.get("restaurant_name")
    if new_name:
        restaurant.name = new_name
        db.commit()
        
    return RedirectResponse(url="/settings", status_code=303)

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