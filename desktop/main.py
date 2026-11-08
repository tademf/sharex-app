# desktop/main.py
import uvicorn

if __name__ == "__main__":
    print("Starting SHAREX Windows Receiver Server on port 8000...")
    uvicorn.run(
        "receiver.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )