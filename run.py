from dotenv import load_dotenv
import os

load_dotenv()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=os.getenv("PORT"), reload=False)
