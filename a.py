from fastapi import WebSocket, WebSocketDisconnect

class KitchenManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, restaurant_id: str, websocket: WebSocket):
        await websocket.accept()
        if restaurant_id not in self.active_connections:
            self.active_connections[restaurant_id] = []
        self.active_connections[restaurant_id].append(websocket)

    async def broadcast_order(self, restaurant_id: str, order_data: dict):
        # Send the order ONLY to the kitchen of this specific restaurant
        if restaurant_id in self.active_connections:
            for connection in self.active_connections[restaurant_id]:
                await connection.send_json(order_data)

manager = KitchenManager()

@app.websocket("/ws/kitchen/{restaurant_id}")
async def websocket_endpoint(websocket: WebSocket, restaurant_id: str):
    await manager.connect(restaurant_id, websocket)
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.active_connections[restaurant_id].remove(websocket)