from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import downloads

app = FastAPI(
    title="YouTube Download API",
    description="API для загрузки видео с YouTube",
    version="1.0.0"
)

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение роутеров
app.include_router(downloads.router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"message": "YouTube Download API работает!"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
