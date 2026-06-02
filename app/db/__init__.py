import warnings

warnings.filterwarnings("ignore", module="paramiko")

from pymongo import MongoClient
import os
import platform
from dotenv import load_dotenv
# pip install "paramiko<3.0.0"

load_dotenv()

# 환경 변수 설정
MONGO_HOST = os.getenv("MONGO_HOST")
MONGO_PORT = int(os.getenv("MONGO_PORT"))
MONGO_USER = os.getenv("MONGO_USERNAME")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
MONGO_AUTH_DB = "admin"

# 2. 우분투 서버 환경: 터널링 없이 로컬 접속
connect_host = MONGO_HOST
connect_port = MONGO_PORT

# 공통: 클라이언트 생성
client = MongoClient(
    f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}"
    f"@{connect_host}:{connect_port}/?authSource={MONGO_AUTH_DB}"
)

db = client["lec-ai"]
users_col = db["users"]
sessions_col = db["sessions"]
history_col = db["history"]
docs_col = db["docs"]
