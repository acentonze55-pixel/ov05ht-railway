from fastapi import FastAPI

app = FastAPI(title="Test Railway Root")

@app.get("/")
async def home():
    return {"status": "ok", "msg": "root attiva", "docs": "/docs"}

@app.get("/__routes")
async def routes():
    return [r.path for r in app.router.routes]
