# app/services/job_manager.py

import uuid
import os
import shutil
from datetime import datetime
from app.core.config import settings
from app.db import history_col  

class JobManager:
    
    @staticmethod
    def reset_interrupted_jobs():
        """
        서버 재시작 시 실행:
        기존에 'processing'이나 'pending' 상태로 남아있던 작업들을 'failed'로 처리합니다.
        (서버가 꺼지면서 작업이 중단된 것으로 간주)
        """
        try:
            history_col.update_many(
                {"status": {"$in": ["processing", "pending"]}},
                {
                    "$set": {
                        "status": "failed",
                        "error": "Server restarted during processing"
                    },
                    "$push": {
                        "logs": "서버 재시작으로 인해 작업이 중단되었습니다."
                    }
                }
            )
        except Exception as e:
            print(f"[ERROR] 작업 상태 초기화 실패: {e}")

    @staticmethod
    def create_job(filename: str, owner: str):
        job_id = str(uuid.uuid4())
        
        new_job = {
            "id": job_id,
            "filename": filename,
            "owner": owner,
            "status": "pending",
            "progress": 0,
            "total_pages": 0,
            "current_page": 0,
            "logs": [],
            "created_at": datetime.now().isoformat(),
            "result_url": None,
            "error": None
        }
        
        history_col.insert_one(new_job)
        return job_id
    
    @staticmethod
    def start_processing(job_id: str):
        history_col.update_one(
            {"id": job_id},
            {"$set": {"status": "processing"}}
        )

    @staticmethod
    def get_jobs_by_user(username: str):
        jobs = list(history_col.find(
            {"owner": username}, 
            {"_id": 0}  
        ).sort("created_at", -1))
        return jobs
    
    @staticmethod
    def get_job(job_id: str):
        return history_col.find_one({"id": job_id}, {"_id": 0})

    @staticmethod
    def get_all_jobs():
        return list(history_col.find({}, {"_id": 0}).sort("created_at", -1))

    @staticmethod
    def update_progress(job_id: str, current: int, total: int, message: str = None):
        # 진행률 계산
        progress = 0
        if total > 0:
            progress = int((current / total) * 100)
            
        update_fields = {
            "current_page": current,
            "total_pages": total,
            "progress": progress
        }
        
        update_query = {"$set": update_fields}
        
        if message:
            log_entry = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
            update_query["$push"] = {"logs": log_entry}
            
        history_col.update_one({"id": job_id}, update_query)

    @staticmethod
    def mark_completed(job_id: str, result_path: str):
        history_col.update_one(
            {"id": job_id},
            {
                "$set": {
                    "status": "completed",
                    "progress": 100,
                    "result_url": result_path
                },
                "$push": {
                    "logs": "작업 완료! 다운로드 가능합니다."
                }
            }
        )

    @staticmethod
    def mark_failed(job_id: str, error_msg: str):
        history_col.update_one(
            {"id": job_id},
            {
                "$set": {
                    "status": "failed",
                    "error": error_msg
                },
                "$push": {
                    "logs": f"에러 발생: {error_msg}"
                }
            }
        )

    @staticmethod
    def delete_job(job_id: str, username: str):
        # 소유자 확인 및 존재 여부 체크
        job = history_col.find_one({"id": job_id, "owner": username})
        if not job:
            return False
            
        history_col.delete_one({"id": job_id})
        
        try:
            result_path = os.path.join(settings.RESULT_DIR, job_id)
            if os.path.exists(result_path):
                shutil.rmtree(result_path)
             
            zip_path = os.path.join(settings.RESULT_DIR, username, f"{job_id}.zip")
            
            if os.path.exists(zip_path):
                os.remove(zip_path)
            
            upload_path = os.path.join(settings.UPLOAD_DIR, job_id)
            if os.path.exists(upload_path):
                shutil.rmtree(upload_path)
        except Exception as e:
            print(f"[WARN] 파일 삭제 중 오류: {e}")
            
        return True

    @staticmethod
    def get_queue_position(job_id: str) -> int:
        current_job = history_col.find_one({"id": job_id})
        if not current_job:
            return 0
            
        target_created_at = current_job['created_at']
        
        count = history_col.count_documents({
            "created_at": {"$lt": target_created_at},
            "status": {"$in": ["pending", "processing"]}
        })
        
        return count
    
    @staticmethod
    def get_user_total_usage(username: str):
        """사용자의 총 누적 요금(USD) 합산"""
        # DB의 모든 completed 작업을 가져와서 토큰 수를 합산 후 비용 계산
        pipeline = [
            {"$match": {"owner": username, "status": "completed"}},
            {"$group": {
                "_id": None,
                "total_prompt": {"$sum": "$cumulative_usage.prompt"},
                "total_completion": {"$sum": "$cumulative_usage.completion"},
                "total_cached": {"$sum": "$cumulative_usage.cached"}
            }}
        ]
        result = list(history_col.aggregate(pipeline))
        if not result:
            return 0.0
            
        return result[0]

# 모듈 로드 시 중단된 작업 상태 초기화
JobManager.reset_interrupted_jobs()