from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def create_merchant_account(db, name, email, password):
    # 1. Hash the password for security
    hashed = pwd_context.hash(password)
    
    # 2. Create the Restaurant record
    new_resto = Restaurant(
        name=name,
        owner_email=email,
        hashed_password=hashed
    )
    db.add(new_resto)
    db.commit()
    db.refresh(new_resto)
    
    # 3. Initialize default items so the dashboard isn't empty
    default_item = MenuItem(
        restaurant_id=new_resto.id,
        name="Sample Dish",
        price=100.0,
        category="Starters"
    )
    db.add(default_item)
    db.commit()
    
    return new_resto